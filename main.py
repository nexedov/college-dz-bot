import html
import time
import threading
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

import config
import subjects

TOKEN = config.TOKEN
OWNERS = [str(x).strip() for x in getattr(config, "OWNERS", [])]
MODERS = [str(x).strip() for x in getattr(config, "MODERS", [])]

bot = telebot.TeleBot(TOKEN)

# Хранилище ID сообщений открытого ДЗ/расписания для автоудаления: {chat_id: [msg_id, ...]}
last_viewed_msgs = {}

# Хранилище ID главного меню для мгновенного обновления галочек: {chat_id: msg_id}
main_menu_msgs = {}

# Состояния ожидания ввода: {chat_id: {"action": str, "key": str, "prompt_mid": int}}
user_states = {}

# Буфер для склейки альбомов (media_group) и единый замок синхронизации потоков
album_buffers = {}
album_lock = threading.Lock()


def is_owner(uid):
    """Главный админ: доступ ко всему + логи + управление правами."""
    return str(uid) in OWNERS


def is_moder(uid):
    """Модератор (или главный админ): доступ к смене расписания и ДЗ."""
    uid = str(uid)
    return uid in OWNERS or uid in MODERS


def can_edit_hw(uid):
    """Право менять ДЗ: главный админ, модераторы и редакторы из базы."""
    uid = str(uid)
    return uid in OWNERS or uid in MODERS or subjects.is_admin(uid)


def clear_previous_view(cid):
    """Удаляет сообщения предыдущего открытого предмета или расписания."""
    mids = last_viewed_msgs.pop(cid, [])
    for mid in mids:
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass


def cancel_user_state(cid):
    """Сбрасывает активное состояние ввода и защищает от залипания."""
    with album_lock:
        state = user_states.pop(cid, None)
    if state and state.get("prompt_mid"):
        try:
            bot.delete_message(cid, state["prompt_mid"])
        except Exception:
            pass
    try:
        bot.clear_step_handler_by_chat_id(cid)
    except Exception:
        pass


def has_homework_content(hw_data):
    """Проверяет, есть ли реальное ДЗ по предмету (текст или файлы)."""
    if not hw_data:
        return False
    txt = hw_data.get("text", "Нет ДЗ").strip().lower()
    files = hw_data.get("files", [])
    return (txt not in ("нет дз", "нет", "-", "пусто", "")) or bool(files)


def get_media(msg):
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id}
    if msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id}
    if msg.voice:
        return {"type": "voice", "file_id": msg.voice.file_id}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id}
    return None


def send_media(cid, item, caption=None, markup=None):
    t = item["type"]
    fid = item["file_id"]
    kw = {}
    if caption:
        kw["caption"] = caption
        kw["parse_mode"] = "HTML"
    if markup:
        kw["reply_markup"] = markup

    if t == "photo":
        return bot.send_photo(cid, fid, **kw)
    elif t == "video":
        return bot.send_video(cid, fid, **kw)
    elif t == "audio":
        return bot.send_audio(cid, fid, **kw)
    elif t == "voice":
        return bot.send_voice(cid, fid, **kw)
    elif t == "document":
        return bot.send_document(cid, fid, **kw)
    return None


def send_grouped_files(cid, files):
    """Объединяет однотипные файлы в альбомы (до 10 шт.) и возвращает список ID сообщений."""
    sent_ids = []
    visuals = [f for f in files if f["type"] in ("photo", "video")]
    docs = [f for f in files if f["type"] == "document"]
    audios = [f for f in files if f["type"] == "audio"]
    voices = [f for f in files if f["type"] == "voice"]

    def send_chunks(items, cls_map):
        for i in range(0, len(items), 10):
            chunk = items[i:i + 10]
            if len(chunk) == 1:
                m = send_media(cid, chunk[0])
                if m:
                    sent_ids.append(m.message_id)
            else:
                media_group = [cls_map[x["type"]](x["file_id"]) for x in chunk]
                msgs = bot.send_media_group(cid, media_group)
                sent_ids.extend([m.message_id for m in msgs])

    send_chunks(visuals, {"photo": types.InputMediaPhoto, "video": types.InputMediaVideo})
    send_chunks(docs, {"document": types.InputMediaDocument})
    send_chunks(audios, {"audio": types.InputMediaAudio})

    for v in voices:
        m = send_media(cid, v)
        if m:
            sent_ids.append(m.message_id)

    return sent_ids


def done_kb(key, uid):
    hw = subjects.get_homework(key)
    done = uid in hw.get("done_by", [])
    txt = "✅ Сделано (убрать)" if done else "☑️ Отметить сделанным"
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(txt, callback_data=f"done_{key}"),
        types.InlineKeyboardButton("✖️ Скрыть", callback_data="close_view")
    )
    return kb


def close_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✖️ Скрыть", callback_data="close_view"))
    return kb


def cancel_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✖️ Отмена", callback_data="cancel_step"))
    return kb


def send_hw(cid, uid, key):
    clear_previous_view(cid)

    _, name = subjects.SUBJECTS[key]
    hw = subjects.get_homework(key)
    txt = html.escape(hw.get("text", "Нет ДЗ"))
    if len(txt) > 3500:
        txt = txt[:3500] + "..."

    upd_at = html.escape(hw.get("updated_at", "-"))
    upd_by = html.escape(hw.get("updated_by", "-"))
    files = hw.get("files", [])

    msg_text = f"<b>{name}</b>\n\n{txt}\n\n<i>Обновлено: {upd_at} ({upd_by})</i>"
    kb = done_kb(key, uid)
    sent_ids = []

    if not files:
        m = bot.send_message(cid, msg_text, parse_mode="HTML", reply_markup=kb)
        sent_ids.append(m.message_id)
    elif len(files) == 1 and len(msg_text) <= 1024:
        m = send_media(cid, files[0], caption=msg_text, markup=kb)
        if m:
            sent_ids.append(m.message_id)
    else:
        sent_ids.extend(send_grouped_files(cid, files))
        m = bot.send_message(
            cid,
            f"{msg_text}\n\n<i>📎 Прикреплено файлов: {len(files)} шт. (выше)</i>",
            parse_mode="HTML",
            reply_markup=kb
        )
        sent_ids.append(m.message_id)

    last_viewed_msgs[cid] = sent_ids


def send_sched(cid):
    clear_previous_view(cid)

    sc = subjects.get_schedule()
    txt = html.escape(sc.get("text", "Расписание пока не скинули"))
    upd_at = html.escape(sc.get("updated_at", "-"))
    upd_by = html.escape(sc.get("updated_by", "-"))
    files = sc.get("files", [])

    msg_text = f"<b>📅 Расписание пар</b>\n\n{txt}\n\n<i>Обновлено: {upd_at} ({upd_by})</i>"
    kb = close_kb()
    sent_ids = []

    if not files:
        m = bot.send_message(cid, msg_text, parse_mode="HTML", reply_markup=kb)
        sent_ids.append(m.message_id)
    elif len(files) == 1 and len(msg_text) <= 1024:
        m = send_media(cid, files[0], caption=msg_text, markup=kb)
        if m:
            sent_ids.append(m.message_id)
    else:
        sent_ids.extend(send_grouped_files(cid, files))
        m = bot.send_message(cid, msg_text, parse_mode="HTML", reply_markup=kb)
        sent_ids.append(m.message_id)

    last_viewed_msgs[cid] = sent_ids


def main_text():
    return "📚 <b>Меню домашнего задания</b>\n<i>📌 — есть ДЗ  |  ✅ — выполнено</i>\n\nВыбери предмет:"


def main_kb(uid):
    uid = str(uid)
    kb = types.InlineKeyboardMarkup(row_width=2)
    data = subjects.load_data()
    sub = data["user_subgroups"].get(uid, 0)

    kb.row(types.InlineKeyboardButton("📅 Расписание", callback_data="view_schedule"))

    btns = []
    for k, (b_txt, _) in subjects.SUBJECTS.items():
        if sub == 1 and k == "eng_2":
            continue
        if sub == 2 and k == "eng_1":
            continue

        hw_data = data["homework"].get(k, {})
        done = uid in hw_data.get("done_by", [])
        has_hw = has_homework_content(hw_data)

        if done:
            pref = "✅ "
        elif has_hw:
            pref = "📌 "
        else:
            pref = ""

        btns.append(types.InlineKeyboardButton(f"{pref}{b_txt}", callback_data=f"view_{k}"))
    kb.add(*btns)

    sub_names = {0: "Подгруппа: Все", 1: "Подгруппа: 1-я", 2: "Подгруппа: 2-я"}
    b_sub = types.InlineKeyboardButton(sub_names[sub], callback_data="toggle_sub")

    if uid in data.get("subscribers", []):
        b_notif = types.InlineKeyboardButton("🔕 Уведомления: ВКЛ", callback_data="toggle_notif")
    else:
        b_notif = types.InlineKeyboardButton("🔔 Уведомления: ВЫКЛ", callback_data="toggle_notif")

    kb.row(b_sub, b_notif)

    # Кнопки для редакторов и модераторов
    if can_edit_hw(uid) and is_moder(uid):
        kb.row(
            types.InlineKeyboardButton("✏️ Изменить ДЗ", callback_data="open_edit"),
            types.InlineKeyboardButton("🔄 Обновить расписание", callback_data="mod_sched")
        )
    elif can_edit_hw(uid):
        kb.row(types.InlineKeyboardButton("✏️ Изменить ДЗ", callback_data="open_edit"))
    elif uid not in data.get("banned_requests", []):
        kb.row(types.InlineKeyboardButton("Подать заявку в редакторы", callback_data="req_admin"))

    # Панель только для Главного админа (OWNERS)
    if is_owner(uid):
        kb.row(types.InlineKeyboardButton("👑 Панель главного админа", callback_data="mod_panel"))

    return kb


def refresh_main_menu(cid, uid):
    """Мгновенно перерисовывает кнопки главного меню, если оно есть в чате."""
    mid = main_menu_msgs.get(cid)
    if mid:
        try:
            bot.edit_message_reply_markup(cid, mid, reply_markup=main_kb(uid))
        except Exception:
            pass


def send_new_main_menu(cid, uid):
    """Отправляет новое главное меню, удаляя старое меню и открытые вкладки."""
    clear_previous_view(cid)
    old_mid = main_menu_msgs.pop(cid, None)
    if old_mid:
        try:
            bot.delete_message(cid, old_mid)
        except Exception:
            pass
    m = bot.send_message(cid, main_text(), parse_mode="HTML", reply_markup=main_kb(uid))
    main_menu_msgs[cid] = m.message_id


def after_edit_kb(key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Докинуть еще файл/альбом", callback_data=f"addfile_{key}"),
        types.InlineKeyboardButton("🗑 Удалить файлы предмета", callback_data=f"clearfiles_{key}"),
        types.InlineKeyboardButton("🔙 Назад в меню", callback_data="back_main")
    )
    return kb


def after_sched_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Добавить еще фото/альбом", callback_data="sched_add_file"),
        types.InlineKeyboardButton("🔙 Назад в меню", callback_data="back_main")
    )
    return kb


@bot.message_handler(commands=['start'])
def start_cmd(message):
    if hasattr(subjects, "track_user"):
        subjects.track_user(message.from_user)
    uid = str(message.from_user.id)
    cid = message.chat.id
    cancel_user_state(cid)
    send_new_main_menu(cid, uid)


@bot.message_handler(commands=['id'])
def id_cmd(message):
    cancel_user_state(message.chat.id)
    bot.send_message(message.chat.id, f"Твой ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@bot.message_handler(commands=['users'])
def users_cmd(message):
    if not is_owner(message.from_user.id):
        return
    users = subjects.load_data().get("users", {})
    if not users:
        bot.send_message(message.chat.id, "Пока никого нет.")
    else:
        lines = [
            f"• {html.escape(u['name'])} ({html.escape(u['username'])}) — <i>{u['last_seen']}</i>"
            for u in users.values()
        ]
        text = f"<b>Всего человек: {len(users)}</b>\n\n" + "\n".join(lines)
        bot.send_message(message.chat.id, text[:4000], parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    if hasattr(subjects, "track_user"):
        subjects.track_user(call.from_user)
    uid = str(call.from_user.id)
    cid = call.message.chat.id
    d = call.data

    if d == "cancel_step":
        cancel_user_state(cid)
        bot.answer_callback_query(call.id, "Действие отменено")
        refresh_main_menu(cid, uid)
        return

    if d == "close_view":
        clear_previous_view(cid)
        bot.answer_callback_query(call.id)
        return

    if d == "view_schedule":
        cancel_user_state(cid)
        main_menu_msgs[cid] = call.message.message_id
        refresh_main_menu(cid, uid)
        send_sched(cid)
        bot.answer_callback_query(call.id)

    elif d.startswith("view_"):
        cancel_user_state(cid)
        main_menu_msgs[cid] = call.message.message_id
        refresh_main_menu(cid, uid)
        key = d.replace("view_", "")
        if key in subjects.SUBJECTS:
            send_hw(cid, uid, key)
        bot.answer_callback_query(call.id)

    elif d.startswith("done_"):
        key = d.replace("done_", "")
        if key in subjects.SUBJECTS:
            res = subjects.toggle_done(key, uid)
            bot.answer_callback_query(call.id, "Отмечено сделанным! ✅" if res else "Отметка снята")
            try:
                bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=done_kb(key, uid))
            except Exception:
                pass
            refresh_main_menu(cid, uid)

    elif d == "toggle_sub":
        cancel_user_state(cid)
        val = subjects.cycle_subgroup(uid)
        info = {0: "Показываются все предметы", 1: "1-я подгруппа", 2: "2-я подгруппа"}
        bot.answer_callback_query(call.id, info[val])
        main_menu_msgs[cid] = call.message.message_id
        bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=main_kb(uid))

    elif d == "toggle_notif":
        cancel_user_state(cid)
        res = subjects.toggle_sub(uid)
        bot.answer_callback_query(call.id, "Уведомления включены" if res else "Уведомления выключены")
        main_menu_msgs[cid] = call.message.message_id
        bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=main_kb(uid))

    elif d == "back_main":
        cancel_user_state(cid)
        clear_previous_view(cid)
        main_menu_msgs[cid] = call.message.message_id
        try:
            bot.edit_message_text(
                main_text(),
                cid,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=main_kb(uid)
            )
        except Exception:
            send_new_main_menu(cid, uid)
        bot.answer_callback_query(call.id)

    elif d == "open_edit":
        cancel_user_state(cid)
        clear_previous_view(cid)
        if not can_edit_hw(uid):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            main_menu_msgs[cid] = call.message.message_id
            refresh_main_menu(cid, uid)
            return

        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [
            types.InlineKeyboardButton(f"✏️ {b_txt}", callback_data=f"edit_{k}")
            for k, (b_txt, _) in subjects.SUBJECTS.items()
        ]
        kb.add(*btns)
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_main"))

        bot.edit_message_text("Что редактируем?", cid, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif d.startswith("edit_"):
        cancel_user_state(cid)
        if not can_edit_hw(uid):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return

        key = d.replace("edit_", "")
        if key in subjects.SUBJECTS:
            _, name = subjects.SUBJECTS[key]
            msg = bot.send_message(
                cid,
                f"Скинь новое ДЗ для <b>{name}</b> (текст, фото, файл или целый альбом).\n"
                f"Для отмены нажми кнопку ниже или напиши «отмена».",
                parse_mode="HTML",
                reply_markup=cancel_kb()
            )
            with album_lock:
                user_states[cid] = {"action": "save_hw", "key": key, "prompt_mid": msg.message_id}
        bot.answer_callback_query(call.id)

    elif d.startswith("addfile_"):
        cancel_user_state(cid)
        if not can_edit_hw(uid):
            return

        key = d.replace("addfile_", "")
        if key in subjects.SUBJECTS:
            _, name = subjects.SUBJECTS[key]
            msg = bot.send_message(
                cid,
                f"Скинь файл, фото или целый альбом для <b>{name}</b>:",
                parse_mode="HTML",
                reply_markup=cancel_kb()
            )
            with album_lock:
                user_states[cid] = {"action": "add_hw_file", "key": key, "prompt_mid": msg.message_id}
        bot.answer_callback_query(call.id)

    elif d.startswith("clearfiles_"):
        cancel_user_state(cid)
        if not can_edit_hw(uid):
            return

        key = d.replace("clearfiles_", "")
        if key in subjects.SUBJECTS:
            subjects.clear_homework_files(key)
            bot.answer_callback_query(call.id, "Файлы удалены", show_alert=True)
            main_menu_msgs[cid] = call.message.message_id
            bot.edit_message_text(
                main_text(),
                cid,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=main_kb(uid)
            )

    elif d == "req_admin":
        if can_edit_hw(uid):
            main_menu_msgs[cid] = call.message.message_id
            try:
                bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=main_kb(uid))
            except Exception:
                send_new_main_menu(cid, uid)
            bot.answer_callback_query(call.id, "У тебя уже есть доступ! Кнопки меню обновлены.", show_alert=True)
            return
        if subjects.is_req_banned(uid):
            bot.answer_callback_query(call.id, "Заявки заблокированы", show_alert=True)
            return
        if not subjects.add_request(uid):
            bot.answer_callback_query(call.id, "Заявка уже отправлена, жди ответа", show_alert=True)
            return

        name = html.escape(call.from_user.first_name or "Без имени")
        uname = f"@{html.escape(call.from_user.username)}" if call.from_user.username else "нет юзернейма"

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("Принять", callback_data=f"app_{uid}"),
            types.InlineKeyboardButton("Отклонить", callback_data=f"rej_{uid}")
        )
        kb.row(types.InlineKeyboardButton("В ЧС", callback_data=f"banreq_{uid}"))

        for owner_id in OWNERS:
            if owner_id.isdigit():
                try:
                    bot.send_message(
                        owner_id,
                        f"Заявка на редактора:\n{name} ({uname})\nID: <code>{uid}</code>",
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                except Exception:
                    pass

        bot.answer_callback_query(call.id, "Заявка отправлена!", show_alert=True)

    elif d.startswith("app_"):
        if not is_owner(uid):
            return
        target = d.replace("app_", "").strip()
        subjects.add_admin(target)
        bot.edit_message_text(f"{call.message.text}\n\n✅ Принято", cid, call.message.message_id)
        try:
            bot.send_message(int(target), "✅ Заявка одобрена! Теперь ты можешь редактировать ДЗ.")
            send_new_main_menu(int(target), target)
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif d.startswith("rej_"):
        if not is_owner(uid):
            return
        target = d.replace("rej_", "").strip()
        subjects.remove_request(target)
        bot.edit_message_text(f"{call.message.text}\n\n❌ Отклонено", cid, call.message.message_id)
        try:
            bot.send_message(int(target), "Твою заявку отклонили.")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif d.startswith("banreq_"):
        if not is_owner(uid):
            return
        target = d.replace("banreq_", "").strip()
        subjects.ban_request(target)
        bot.edit_message_text(f"{call.message.text}\n\n🚫 В ЧС", cid, call.message.message_id)
        bot.answer_callback_query(call.id, "Заблокирован")

    elif d == "mod_panel":
        cancel_user_state(cid)
        clear_previous_view(cid)
        if not is_owner(uid):
            bot.answer_callback_query(call.id, "Доступно только главному админу", show_alert=True)
            return

        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("📅 Обновить расписание", callback_data="mod_sched"),
            types.InlineKeyboardButton("📊 Кто пользуется (Статистика)", callback_data="mod_users"),
            types.InlineKeyboardButton("➕ Добавить редактора по ID", callback_data="mod_add"),
            types.InlineKeyboardButton("➖ Снять редактора по ID", callback_data="mod_del"),
            types.InlineKeyboardButton("📋 Список ролей и админов", callback_data="mod_list"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="back_main")
        )
        bot.edit_message_text("👑 Панель главного админа:", cid, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif d == "mod_users":
        if not is_owner(uid):
            return
        users = subjects.load_data().get("users", {})
        if not users:
            bot.send_message(cid, "Пока никого нет.")
        else:
            lines = [
                f"• {html.escape(u['name'])} ({html.escape(u['username'])}) — <i>{u['last_seen']}</i>"
                for u in users.values()
            ]
            text = f"<b>Всего человек: {len(users)}</b>\n\n" + "\n".join(lines)
            bot.send_message(cid, text[:4000], parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif d == "mod_sched":
        cancel_user_state(cid)
        if not is_moder(uid):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        msg = bot.send_message(
            cid,
            "Скинь фото/альбом расписания (можно с подписью) или текст:",
            reply_markup=cancel_kb()
        )
        with album_lock:
            user_states[cid] = {"action": "save_sched", "key": None, "prompt_mid": msg.message_id}
        bot.answer_callback_query(call.id)

    elif d == "sched_add_file":
        cancel_user_state(cid)
        if not is_moder(uid):
            return
        msg = bot.send_message(
            cid,
            "Скинь еще фото или альбом к расписанию:",
            reply_markup=cancel_kb()
        )
        with album_lock:
            user_states[cid] = {"action": "add_sched_file", "key": None, "prompt_mid": msg.message_id}
        bot.answer_callback_query(call.id)

    elif d == "mod_list":
        if not is_owner(uid):
            return
        users = subjects.load_data().get("users", {})

        def fmt_list(id_list):
            res = []
            for a in id_list:
                if not str(a).isdigit():
                    continue
                u = users.get(str(a))
                if u:
                    res.append(f"• {html.escape(u['name'])} ({html.escape(u['username'])}) — <code>{a}</code>")
                else:
                    res.append(f"• <code>{a}</code>")
            return "\n".join(res) or "Пусто"

        db_admins = [a for a in subjects.get_admins() if a.isdigit() and a not in OWNERS and a not in MODERS]
        text = (
            f"👑 <b>Главный админ:</b>\n{fmt_list(OWNERS)}\n\n"
            f"🛡 <b>Модераторы (ДЗ + Расписание):</b>\n{fmt_list(MODERS)}\n\n"
            f"✏️ <b>Редакторы ДЗ:</b>\n{fmt_list(db_admins)}"
        )
        bot.send_message(cid, text, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif d == "mod_add":
        cancel_user_state(cid)
        if not is_owner(uid):
            return
        msg = bot.send_message(cid, "Введи Telegram ID нового редактора ДЗ:", reply_markup=cancel_kb())
        with album_lock:
            user_states[cid] = {"action": "add_admin", "key": None, "prompt_mid": msg.message_id}
        bot.answer_callback_query(call.id)

    elif d == "mod_del":
        cancel_user_state(cid)
        if not is_owner(uid):
            return
        msg = bot.send_message(cid, "Введи Telegram ID для снятия прав редактора:", reply_markup=cancel_kb())
        with album_lock:
            user_states[cid] = {"action": "del_admin", "key": None, "prompt_mid": msg.message_id}
        bot.answer_callback_query(call.id)


def execute_state_action(cid, uid, user, action, key, txt, medias):
    """Выполняет сохранение ДЗ, расписания или админов (одиночное сообщение или собранный альбом)."""
    if action == "save_hw":
        if not txt and not medias:
            bot.send_message(cid, "Отправь текст или прикрепи файл.", reply_markup=main_kb(uid))
            return
        if not txt and medias:
            txt = "В прикрепленном файле"

        author = user.first_name or "Админ"
        first_media = medias[0] if medias else None
        subjects.update_homework(key, txt, author, first_media)
        for extra_media in medias[1:]:
            subjects.add_homework_file(key, extra_media, author)

        _, name = subjects.SUBJECTS[key]
        safe_txt = html.escape(txt)
        files_note = f"\n<i>📎 Прикреплено файлов: {len(medias)} шт.</i>" if medias else ""

        m = bot.send_message(
            cid,
            f"✅ ДЗ по предмету <b>{name}</b> сохранено:\n\n{safe_txt}{files_note}",
            parse_mode="HTML",
            reply_markup=after_edit_kb(key)
        )
        main_menu_msgs[cid] = m.message_id

        log_to_owners(user, f"изменено ДЗ ({name})", safe_txt, medias)
        broadcast(uid, f"<b>Обновлено ДЗ: {name}</b>", safe_txt, medias, key)

    elif action == "add_hw_file":
        if not medias:
            bot.send_message(cid, "Прикрепи фото, видео или документ.", reply_markup=after_edit_kb(key))
            return

        author = user.first_name or "Админ"
        cnt = 0
        for m_item in medias:
            cnt = subjects.add_homework_file(key, m_item, author)
        _, name = subjects.SUBJECTS[key]

        m = bot.send_message(
            cid,
            f"📎 Файлы добавлены к <b>{name}</b> (+{len(medias)} шт., всего: {cnt})",
            parse_mode="HTML",
            reply_markup=after_edit_kb(key)
        )
        main_menu_msgs[cid] = m.message_id

        log_to_owners(user, f"добавлены файлы к ДЗ ({name})", f"Добавлено файлов: {len(medias)} шт.", medias)
        broadcast(uid, f"<b>Новые файлы к ДЗ: {name}</b>", "", medias, key)

    elif action == "save_sched":
        if not txt and not medias:
            bot.send_message(cid, "Нужно скинуть фото или текст.", reply_markup=main_kb(uid))
            return
        if not txt and medias:
            txt = "Расписание на фото"

        author = user.first_name or "Модератор"
        first_media = medias[0] if medias else None
        subjects.update_schedule(txt, author, first_media)
        for extra_media in medias[1:]:
            subjects.add_schedule_file(extra_media, author)

        safe_txt = html.escape(txt)
        m = bot.send_message(
            cid,
            f"✅ Расписание обновлено! (Файлов: {len(medias)})",
            reply_markup=after_sched_kb()
        )
        main_menu_msgs[cid] = m.message_id

        log_to_owners(user, "обновлено расписание", safe_txt, medias)
        broadcast(uid, "📅 <b>Новое расписание пар</b>", safe_txt, medias)

    elif action == "add_sched_file":
        if not medias:
            bot.send_message(cid, "Скинь фото или файл.", reply_markup=after_sched_kb())
            return

        author = user.first_name or "Модератор"
        cnt = 0
        for m_item in medias:
            cnt = subjects.add_schedule_file(m_item, author)

        m = bot.send_message(
            cid,
            f"📎 Добавлено (+{len(medias)} шт., всего файлов: {cnt})",
            reply_markup=after_sched_kb()
        )
        main_menu_msgs[cid] = m.message_id

        log_to_owners(user, "добавлены файлы к расписанию", f"Файлов: +{len(medias)} шт.", medias)
        broadcast(uid, "📅 <b>Дополнение к расписанию</b>", "", medias)

    elif action == "add_admin":
        tid = txt.strip()
        if not tid.isdigit():
            bot.send_message(cid, "ID должен состоять только из цифр.")
            return
        if subjects.add_admin(tid):
            bot.send_message(cid, f"✅ Права редактора ДЗ выданы: <code>{tid}</code>", parse_mode="HTML")
            try:
                bot.send_message(int(tid), "✅ Тебе выдали права редактора ДЗ!")
                send_new_main_menu(int(tid), tid)
            except Exception:
                pass
        else:
            bot.send_message(cid, "Этот ID уже в списке редакторов. Ему отправлено обновленное меню.")
            try:
                send_new_main_menu(int(tid), tid)
            except Exception:
                pass

    elif action == "del_admin":
        tid = txt.strip()
        if tid in OWNERS or tid in MODERS:
            bot.send_message(cid, "Главного админа или модератора из config.py нельзя снять через бота.")
            return
        if subjects.remove_admin(tid):
            bot.send_message(cid, f"🗑 Права редактора сняты: <code>{tid}</code>", parse_mode="HTML")
            try:
                send_new_main_menu(int(tid), tid)
            except Exception:
                pass
        else:
            bot.send_message(cid, "Такого ID нет в списке редакторов.")


def finalize_album(mg_id):
    """Вызывается по таймеру, когда все файлы одного альбома дошли до бота."""
    with album_lock:
        buf = album_buffers.pop(mg_id, None)
    if not buf:
        return

    # Сортируем файлы по порядку их message_id, чтобы кадры альбома не путались
    sorted_items = sorted(buf["items"], key=lambda x: x[0])
    medias = [item[1] for item in sorted_items]

    execute_state_action(
        cid=buf["cid"],
        uid=buf["uid"],
        user=buf["user"],
        action=buf["action"],
        key=buf["key"],
        txt=buf["txt"],
        medias=medias
    )


@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'voice'])
def handle_all_inputs(message):
    cid = message.chat.id
    uid = str(message.from_user.id)
    txt = (message.text or message.caption or "").strip()
    media = get_media(message)
    mg_id = message.media_group_id

    prompt_to_delete = None

    with album_lock:
        # 1. Если это продолжение альбома, который уже собирается в буфер
        if mg_id and mg_id in album_buffers:
            buf = album_buffers[mg_id]
            if txt and not buf["txt"]:
                buf["txt"] = txt
            if media:
                buf["items"].append((message.message_id, media))
            if buf["timer"]:
                buf["timer"].cancel()
            t = threading.Timer(1.2, finalize_album, args=[mg_id])
            buf["timer"] = t
            t.start()
            return

        # 2. Проверяем, ждет ли бот ввода от этого пользователя
        if cid not in user_states:
            return

        # Защита от залипания: отмена по слову «отмена» или любой команде через /
        if txt.lower() == "отмена" or txt.startswith("/"):
            state = user_states.pop(cid, None)
            prompt_to_delete = state.get("prompt_mid") if state else None
            is_cancel = True
        else:
            is_cancel = False
            state = user_states.pop(cid)
            prompt_to_delete = state.get("prompt_mid")
            action = state["action"]
            key = state["key"]

            # 3. Если сообщение — первая часть альбома, сразу создаем буфер ВНУТРИ замка
            if mg_id and action in ("save_hw", "add_hw_file", "save_sched", "add_sched_file"):
                t = threading.Timer(1.2, finalize_album, args=[mg_id])
                album_buffers[mg_id] = {
                    "cid": cid,
                    "uid": uid,
                    "user": message.from_user,
                    "action": action,
                    "key": key,
                    "txt": txt,
                    "items": [(message.message_id, media)] if media else [],
                    "timer": t
                }
                t.start()
                is_album_started = True
            else:
                is_album_started = False

    # Удаляем сообщение-подсказку вне блокировки, чтобы не задерживать соседние потоки альбома
    if prompt_to_delete:
        try:
            bot.delete_message(cid, prompt_to_delete)
        except Exception:
            pass

    if is_cancel:
        send_new_main_menu(cid, uid)
        return

    if is_album_started:
        return

    # 4. Обычное одиночное сообщение (текст или 1 файл)
    medias = [media] if media else []
    execute_state_action(cid, uid, message.from_user, action, key, txt, medias)


def log_to_owners(user, action_title, safe_txt, medias=None):
    """Отправляет логи изменений ТОЛЬКО главным админам (OWNERS)."""
    uid = str(user.id)
    name = html.escape(user.first_name or "Юзер")
    uname = f"@{html.escape(user.username)}" if user.username else "без тега"
    log_msg = f"📋 <b>Лог: {action_title}</b>\nКто: {name} ({uname}, <code>{uid}</code>)\n\n{safe_txt[:800]}"
    medias = medias or []

    for owner_id in OWNERS:
        if not owner_id.isdigit():
            continue
        try:
            if len(medias) == 1 and len(log_msg) <= 1024:
                send_media(owner_id, medias[0], caption=log_msg)
            elif len(medias) > 1:
                send_grouped_files(owner_id, medias)
                bot.send_message(owner_id, log_msg, parse_mode="HTML")
            else:
                bot.send_message(owner_id, log_msg, parse_mode="HTML")
                if medias:
                    send_media(owner_id, medias[0])
        except Exception:
            pass


def broadcast(author_id, header, safe_txt, medias=None, subj_key=None):
    data = subjects.load_data()
    subs = data.get("subscribers", [])
    groups = data.get("user_subgroups", {})
    medias = medias or []

    body = f"{header}\n\n{safe_txt[:900]}" if safe_txt else header

    for s_id in list(subs):
        if s_id == author_id or not s_id.isdigit():
            continue
        # Не дублируем уведомление главному админу, так как ему уже пришел лог
        if s_id in OWNERS:
            continue

        # Скипаем чужую подгруппу
        g = groups.get(s_id, 0)
        if subj_key == "eng_2" and g == 1:
            continue
        if subj_key == "eng_1" and g == 2:
            continue

        try:
            if len(medias) == 1 and len(body) <= 1024:
                send_media(s_id, medias[0], caption=body)
            elif len(medias) > 1:
                send_grouped_files(s_id, medias)
                bot.send_message(s_id, body, parse_mode="HTML")
            else:
                bot.send_message(s_id, body, parse_mode="HTML")
                if medias:
                    send_media(s_id, medias[0])
            time.sleep(0.05)
        except ApiTelegramException as e:
            if e.error_code in (403, 400):
                subjects.remove_sub(s_id)
        except Exception:
            pass


if __name__ == "__main__":
    subjects.load_data()
    print("bot started")
    bot.infinity_polling(skip_pending=True)
