import html
import time
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from config import TOKEN, MODERS
import subjects

bot = telebot.TeleBot(TOKEN)


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
        bot.send_photo(cid, fid, **kw)
    elif t == "video":
        bot.send_video(cid, fid, **kw)
    elif t == "audio":
        bot.send_audio(cid, fid, **kw)
    elif t == "voice":
        bot.send_voice(cid, fid, **kw)
    elif t == "document":
        bot.send_document(cid, fid, **kw)


def done_kb(key, uid):
    hw = subjects.get_homework(key)
    done = uid in hw.get("done_by", [])
    txt = "✅ Сделано (убрать)" if done else "☑️ Отметить сделанным"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(txt, callback_data=f"done_{key}"))
    return kb


def send_hw(cid, uid, key):
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

    if not files:
        bot.send_message(cid, msg_text, parse_mode="HTML", reply_markup=kb)
    elif len(files) == 1 and len(msg_text) <= 1024:
        send_media(cid, files[0], caption=msg_text, markup=kb)
    else:
        bot.send_message(cid, f"{msg_text}\n\n<i>Файлы ({len(files)} шт.):</i>", parse_mode="HTML", reply_markup=kb)
        for f in files:
            send_media(cid, f)


def send_sched(cid):
    sc = subjects.get_schedule()
    txt = html.escape(sc.get("text", "Расписание пока не скинули"))
    upd_at = html.escape(sc.get("updated_at", "-"))
    upd_by = html.escape(sc.get("updated_by", "-"))
    files = sc.get("files", [])

    msg_text = f"<b>Расписание пар</b>\n\n{txt}\n\n<i>Обновлено: {upd_at} ({upd_by})</i>"

    if not files:
        bot.send_message(cid, msg_text, parse_mode="HTML")
    elif len(files) == 1 and len(msg_text) <= 1024:
        send_media(cid, files[0], caption=msg_text)
    else:
        bot.send_message(cid, msg_text, parse_mode="HTML")
        for f in files:
            send_media(cid, f)


def main_kb(uid):
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

        done = uid in data["homework"].get(k, {}).get("done_by", [])
        pref = "✅ " if done else ""
        btns.append(types.InlineKeyboardButton(f"{pref}{b_txt}", callback_data=f"view_{k}"))
    kb.add(*btns)

    sub_names = {0: "Подгруппа: Все", 1: "Подгруппа: 1-я", 2: "Подгруппа: 2-я"}
    b_sub = types.InlineKeyboardButton(sub_names[sub], callback_data="toggle_sub")

    if uid in data.get("subscribers", []):
        b_notif = types.InlineKeyboardButton("🔕 Уведомления: ВКЛ", callback_data="toggle_notif")
    else:
        b_notif = types.InlineKeyboardButton("🔔 Уведомления: ВЫКЛ", callback_data="toggle_notif")

    kb.row(b_sub, b_notif)

    if subjects.is_admin(uid):
        kb.row(types.InlineKeyboardButton("✏️ Изменить ДЗ", callback_data="open_edit"))
    elif uid not in data.get("banned_requests", []):
        kb.row(types.InlineKeyboardButton("Подать заявку в редакторы", callback_data="req_admin"))

    if uid in MODERS:
        kb.row(types.InlineKeyboardButton("Панель модератора", callback_data="mod_panel"))

    return kb


def after_edit_kb(key):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Докинуть еще файл", callback_data=f"addfile_{key}"),
        types.InlineKeyboardButton("🗑 Удалить файлы предмета", callback_data=f"clearfiles_{key}"),
        types.InlineKeyboardButton("Назад в меню", callback_data="back_main")
    )
    return kb


def after_sched_kb():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Добавить еще фото", callback_data="sched_add_file"),
        types.InlineKeyboardButton("Назад в меню", callback_data="back_main")
    )
    return kb


@bot.message_handler(commands=['start'])
def start_cmd(message):
    uid = str(message.from_user.id)
    bot.send_message(message.chat.id, "Выбери предмет:", reply_markup=main_kb(uid))


@bot.message_handler(commands=['id'])
def id_cmd(message):
    bot.send_message(message.chat.id, f"Твой ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid = str(call.from_user.id)
    cid = call.message.chat.id
    d = call.data

    if d == "view_schedule":
        send_sched(cid)
        bot.answer_callback_query(call.id)

    elif d.startswith("view_"):
        key = d.replace("view_", "")
        if key in subjects.SUBJECTS:
            send_hw(cid, uid, key)
        bot.answer_callback_query(call.id)

    elif d.startswith("done_"):
        key = d.replace("done_", "")
        if key in subjects.SUBJECTS:
            res = subjects.toggle_done(key, uid)
            bot.answer_callback_query(call.id, "Отмечено!" if res else "Отметка снята")
            try:
                bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=done_kb(key, uid))
            except Exception:
                pass

    elif d == "toggle_sub":
        val = subjects.cycle_subgroup(uid)
        info = {0: "Показываются все предметы", 1: "1-я подгруппа", 2: "2-я подгруппа"}
        bot.answer_callback_query(call.id, info[val])
        bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=main_kb(uid))

    elif d == "toggle_notif":
        res = subjects.toggle_sub(uid)
        bot.answer_callback_query(call.id, "Уведомления включены" if res else "Уведомления выключены")
        bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=main_kb(uid))

    elif d == "back_main":
        bot.edit_message_text("Выбери предмет:", cid, call.message.message_id, reply_markup=main_kb(uid))
        bot.answer_callback_query(call.id)

    elif d == "open_edit":
        if not subjects.is_admin(uid):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return

        kb = types.InlineKeyboardMarkup(row_width=2)
        btns = [
            types.InlineKeyboardButton(f"✏️ {b_txt}", callback_data=f"edit_{k}")
            for k, (b_txt, _) in subjects.SUBJECTS.items()
        ]
        kb.add(*btns)
        kb.add(types.InlineKeyboardButton("Назад", callback_data="back_main"))

        bot.edit_message_text("Что редактируем?", cid, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif d.startswith("edit_"):
        if not subjects.is_admin(uid):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return

        key = d.replace("edit_", "")
        if key in subjects.SUBJECTS:
            _, name = subjects.SUBJECTS[key]
            msg = bot.send_message(
                cid,
                f"Скинь новое ДЗ для <b>{name}</b> (текст, фото или файл).\nДля отмены напиши «отмена».",
                parse_mode="HTML"
            )
            bot.register_next_step_handler(msg, save_hw_step, key)
        bot.answer_callback_query(call.id)

    elif d.startswith("addfile_"):
        if not subjects.is_admin(uid):
            return

        key = d.replace("addfile_", "")
        if key in subjects.SUBJECTS:
            _, name = subjects.SUBJECTS[key]
            msg = bot.send_message(
                cid,
                f"Скинь файл или фото для <b>{name}</b> (или «отмена»):",
                parse_mode="HTML"
            )
            bot.register_next_step_handler(msg, add_file_step, key)
        bot.answer_callback_query(call.id)

    elif d.startswith("clearfiles_"):
        if not subjects.is_admin(uid):
            return

        key = d.replace("clearfiles_", "")
        if key in subjects.SUBJECTS:
            subjects.clear_homework_files(key)
            bot.answer_callback_query(call.id, "Файлы удалены", show_alert=True)
            bot.edit_message_text("Выбери предмет:", cid, call.message.message_id, reply_markup=main_kb(uid))

    elif d == "req_admin":
        if subjects.is_admin(uid):
            bot.answer_callback_query(call.id, "У тебя уже есть доступ", show_alert=True)
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

        for m in MODERS:
            if m.isdigit():
                try:
                    bot.send_message(
                        m,
                        f"Заявка на редактора:\n{name} ({uname})\nID: <code>{uid}</code>",
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                except Exception:
                    pass

        bot.answer_callback_query(call.id, "Заявка отправлена!", show_alert=True)

    elif d.startswith("app_"):
        if uid not in MODERS:
            return
        target = d.replace("app_", "")
        subjects.add_admin(target)
        bot.edit_message_text(f"{call.message.text}\n\n✅ Принято", cid, call.message.message_id)
        try:
            bot.send_message(target, "Заявка одобрена, теперь можешь редактировать ДЗ.", reply_markup=main_kb(target))
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif d.startswith("rej_"):
        if uid not in MODERS:
            return
        target = d.replace("rej_", "")
        subjects.remove_request(target)
        bot.edit_message_text(f"{call.message.text}\n\n❌ Отклонено", cid, call.message.message_id)
        try:
            bot.send_message(target, "Твою заявку отклонили.")
        except Exception:
            pass
        bot.answer_callback_query(call.id)

    elif d.startswith("banreq_"):
        if uid not in MODERS:
            return
        target = d.replace("banreq_", "")
        subjects.ban_request(target)
        bot.edit_message_text(f"{call.message.text}\n\n🚫 В ЧС", cid, call.message.message_id)
        bot.answer_callback_query(call.id, "Заблокирован")

    elif d == "mod_panel":
        if uid not in MODERS:
            return

        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("Обновить расписание", callback_data="mod_sched"),
            types.InlineKeyboardButton("Добавить админа по ID", callback_data="mod_add"),
            types.InlineKeyboardButton("Снять админа по ID", callback_data="mod_del"),
            types.InlineKeyboardButton("Список админов", callback_data="mod_list"),
            types.InlineKeyboardButton("Назад", callback_data="back_main")
        )
        bot.edit_message_text("Панель модератора:", cid, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif d == "mod_sched":
        if uid not in MODERS:
            return
        msg = bot.send_message(cid, "Скинь фото расписания (можно с текстом) или напиши «отмена»:")
        bot.register_next_step_handler(msg, save_sched_step)
        bot.answer_callback_query(call.id)

    elif d == "sched_add_file":
        if uid not in MODERS:
            return
        msg = bot.send_message(cid, "Скинь еще фото к расписанию (или «отмена»):")
        bot.register_next_step_handler(msg, add_sched_file_step)
        bot.answer_callback_query(call.id)

    elif d == "mod_list":
        if uid not in MODERS:
            return
        adm = subjects.get_admins()
        txt = "\n".join([f"- <code>{a}</code>" for a in adm]) or "Пусто"
        bot.send_message(cid, f"Админы:\n{txt}", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif d == "mod_add":
        if uid not in MODERS:
            return
        msg = bot.send_message(cid, "Введи ID:")
        bot.register_next_step_handler(msg, add_admin_step)
        bot.answer_callback_query(call.id)

    elif d == "mod_del":
        if uid not in MODERS:
            return
        msg = bot.send_message(cid, "Введи ID для снятия:")
        bot.register_next_step_handler(msg, del_admin_step)
        bot.answer_callback_query(call.id)


def save_sched_step(message):
    uid = str(message.from_user.id)
    media = get_media(message)
    txt = (message.text or message.caption or "").strip()

    if txt.lower() == "отмена":
        bot.send_message(message.chat.id, "Отменено", reply_markup=main_kb(uid))
        return

    if not txt and not media:
        bot.send_message(message.chat.id, "Нужно скинуть фото или текст.")
        return

    if not txt and media:
        txt = "Расписание на фото"

    author = message.from_user.first_name or "Модератор"
    subjects.update_schedule(txt, author, media)
    safe_txt = html.escape(txt)

    bot.send_message(message.chat.id, "Расписание обновлено!", reply_markup=after_sched_kb())
    broadcast(uid, "📅 <b>Новое расписание пар</b>", safe_txt, media)


def add_sched_file_step(message):
    uid = str(message.from_user.id)
    if message.text and message.text.strip().lower() == "отмена":
        bot.send_message(message.chat.id, "Отменено", reply_markup=main_kb(uid))
        return

    media = get_media(message)
    if not media:
        bot.send_message(message.chat.id, "Скинь фото или файл.", reply_markup=after_sched_kb())
        return

    author = message.from_user.first_name or "Модератор"
    cnt = subjects.add_schedule_file(media, author)
    bot.send_message(message.chat.id, f"Добавлено (всего файлов: {cnt})", reply_markup=after_sched_kb())
    broadcast(uid, "📅 <b>Дополнение к расписанию</b>", "", media)


def save_hw_step(message, key):
    uid = str(message.from_user.id)
    media = get_media(message)
    txt = (message.text or message.caption or "").strip()

    if txt.lower() == "отмена":
        bot.send_message(message.chat.id, "Отменено", reply_markup=main_kb(uid))
        return

    if not txt and not media:
        bot.send_message(message.chat.id, "Отправь текст или прикрепи файл.")
        return

    if not txt and media:
        txt = "В прикрепленном файле"

    author = message.from_user.first_name or "Админ"
    subjects.update_homework(key, txt, author, media)
    _, name = subjects.SUBJECTS[key]
    safe_txt = html.escape(txt)

    bot.send_message(
        message.chat.id,
        f"ДЗ по предмету <b>{name}</b> сохранено:\n\n{safe_txt}",
        parse_mode="HTML",
        reply_markup=after_edit_kb(key)
    )

    log_to_moders(message.from_user, name, safe_txt, media)
    broadcast(uid, f"<b>Обновлено ДЗ: {name}</b>", safe_txt, media, key)


def add_file_step(message, key):
    uid = str(message.from_user.id)
    if message.text and message.text.strip().lower() == "отмена":
        bot.send_message(message.chat.id, "Отменено", reply_markup=main_kb(uid))
        return

    media = get_media(message)
    if not media:
        bot.send_message(message.chat.id, "Прикрепи фото, видео или документ.", reply_markup=after_edit_kb(key))
        return

    author = message.from_user.first_name or "Админ"
    cnt = subjects.add_homework_file(key, media, author)
    _, name = subjects.SUBJECTS[key]

    bot.send_message(
        message.chat.id,
        f"Файл прикреплен к <b>{name}</b> (всего: {cnt})",
        parse_mode="HTML",
        reply_markup=after_edit_kb(key)
    )

    log_to_moders(message.from_user, name, "Добавлен файл", media)
    broadcast(uid, f"<b>Новый файл к ДЗ: {name}</b>", "", media, key)


def log_to_moders(user, subj_name, safe_txt, media=None):
    uid = str(user.id)
    name = html.escape(user.first_name or "Юзер")
    uname = f"@{html.escape(user.username)}" if user.username else "без тега"
    log_msg = f"Лог: изменено ДЗ ({subj_name})\nКто: {name} ({uname}, <code>{uid}</code>)\n\n{safe_txt[:800]}"

    for m in MODERS:
        if not m.isdigit():
            continue
        try:
            if media and len(log_msg) <= 1024:
                send_media(m, media, caption=log_msg)
            else:
                bot.send_message(m, log_msg, parse_mode="HTML")
                if media:
                    send_media(m, media)
        except Exception:
            pass


def broadcast(author_id, header, safe_txt, media=None, subj_key=None):
    data = subjects.load_data()
    subs = data.get("subscribers", [])
    groups = data.get("user_subgroups", {})

    body = f"{header}\n\n{safe_txt[:900]}" if safe_txt else header

    for s_id in list(subs):
        if s_id == author_id or not s_id.isdigit():
            continue
        if subj_key and s_id in MODERS:
            continue

        # скипаем чужую подгруппу
        g = groups.get(s_id, 0)
        if subj_key == "eng_2" and g == 1:
            continue
        if subj_key == "eng_1" and g == 2:
            continue

        try:
            if media and len(body) <= 1024:
                send_media(s_id, media, caption=body)
            else:
                bot.send_message(s_id, body, parse_mode="HTML")
                if media:
                    send_media(s_id, media)
            time.sleep(0.05)
        except ApiTelegramException as e:
            if e.error_code in (403, 400):
                subjects.remove_sub(s_id)
        except Exception:
            pass


def add_admin_step(message):
    if not message.text:
        return
    tid = message.text.strip()
    if not tid.isdigit():
        bot.send_message(message.chat.id, "ID должен быть из цифр.")
        return

    if subjects.add_admin(tid):
        bot.send_message(message.chat.id, f"Админка выдана: {tid}")
    else:
        bot.send_message(message.chat.id, "Уже в списке админов.")


def del_admin_step(message):
    if not message.text:
        return
    tid = message.text.strip()
    if tid in MODERS:
        bot.send_message(message.chat.id, "Модератора снять нельзя.")
        return

    if subjects.remove_admin(tid):
        bot.send_message(message.chat.id, f"Админка снята: {tid}")
    else:
        bot.send_message(message.chat.id, "Такого ID нет в админах.")


if __name__ == "__main__":
    subjects.load_data()
    print("bot started")
    bot.infinity_polling(skip_pending=True)