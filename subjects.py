import json
import os
import threading
from datetime import datetime
from config import DATA_FILE, INITIAL_ADMINS, MODERS

lock = threading.Lock()

SUBJECTS = {
    "aas": ("🖥 ААС", "ААС (Архитектура аппаратных систем)"),
    "oaip": ("💻 ОА и П", "ОА и П (Основы алгоритмизации и программирования)"),
    "it": ("🌐 ИТ", "ИТ (Информационные технологии)"),
    "osos": ("⚙️ ОС и ОС", "ОС и ОС (Операционные системы и среды)"),
    "kg": ("🎨 КГ", "КГ (Компьютерная графика)"),
    "kp": ("🗣 КП", "КП (Коммуникативный практикум)"),
    "evm": ("📐 ЭВМ", "ЭВМ (Элементы высшей математики)"),
    "eng_1": ("🇬🇧 ИЯ (1 гр.)", "ИЯ (Английский язык — 1 группа)"),
    "eng_2": ("🇬🇧 ИЯ (2 гр.)", "ИЯ (Английский язык — 2 группа)"),
    "bzhd": ("⛑ БЖД", "БЖД (Безопасность жизнедеятельности)")
}


def now_time():
    return datetime.now().strftime("%d.%m в %H:%M")


def load_data():
    with lock:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}

        changed = False

        if "homework" not in data:
            data["homework"] = {}
            changed = True

        for k in SUBJECTS:
            if k not in data["homework"] or isinstance(data["homework"][k], str):
                old = data["homework"].get(k, "Нет ДЗ") if isinstance(data["homework"].get(k), str) else "Нет ДЗ"
                data["homework"][k] = {
                    "text": old,
                    "files": [],
                    "updated_at": "недавно",
                    "updated_by": "-",
                    "done_by": []
                }
                changed = True
            else:
                for field, default in [("updated_at", "-"), ("updated_by", "-"), ("done_by", [])]:
                    if field not in data["homework"][k]:
                        data["homework"][k][field] = default
                        changed = True

        if "schedule" not in data:
            data["schedule"] = {
                "text": "Расписание пока не скинули",
                "files": [],
                "updated_at": "-",
                "updated_by": "-"
            }
            changed = True

        if "admins" not in data:
            data["admins"] = list(set(INITIAL_ADMINS + MODERS))
            changed = True
        else:
            for m in MODERS:
                if m not in data["admins"]:
                    data["admins"].append(m)
                    changed = True

        for key in ("pending_requests", "banned_requests", "subscribers"):
            if key not in data:
                data[key] = []
                changed = True

        if "user_subgroups" not in data:
            data["user_subgroups"] = {}
            changed = True

        if changed:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

        return data


def save_data(data):
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)


def get_homework(key):
    data = load_data()
    return data["homework"].get(key, {
        "text": "Нет ДЗ", "files": [], "updated_at": "-", "updated_by": "-", "done_by": []
    })


def update_homework(key, text, author, media=None):
    if key not in SUBJECTS:
        return False
    data = load_data()
    data["homework"][key] = {
        "text": text,
        "files": [media] if media else [],
        "updated_at": now_time(),
        "updated_by": author,
        "done_by": []
    }
    save_data(data)
    return True


def add_homework_file(key, media, author):
    data = load_data()
    if key in data["homework"]:
        data["homework"][key]["files"].append(media)
        data["homework"][key]["updated_at"] = now_time()
        data["homework"][key]["updated_by"] = author
        save_data(data)
        return len(data["homework"][key]["files"])
    return 0


def clear_homework_files(key):
    data = load_data()
    if key in data["homework"]:
        data["homework"][key]["files"] = []
        save_data(data)


def toggle_done(key, uid):
    data = load_data()
    if key not in data["homework"]:
        return False
    done_list = data["homework"][key].setdefault("done_by", [])
    if uid in done_list:
        done_list.remove(uid)
        res = False
    else:
        done_list.append(uid)
        res = True
    save_data(data)
    return res


def get_schedule():
    return load_data()["schedule"]


def update_schedule(text, author, media=None):
    data = load_data()
    data["schedule"] = {
        "text": text,
        "files": [media] if media else [],
        "updated_at": now_time(),
        "updated_by": author
    }
    save_data(data)


def add_schedule_file(media, author):
    data = load_data()
    data["schedule"]["files"].append(media)
    data["schedule"]["updated_at"] = now_time()
    data["schedule"]["updated_by"] = author
    save_data(data)
    return len(data["schedule"]["files"])


def cycle_subgroup(uid):
    data = load_data()
    cur = data["user_subgroups"].get(uid, 0)
    nxt = (cur + 1) % 3
    data["user_subgroups"][uid] = nxt
    save_data(data)
    return nxt


def get_admins():
    return load_data()["admins"]


def is_admin(uid):
    return uid in get_admins() or uid in MODERS


def add_admin(uid):
    data = load_data()
    if uid in data["pending_requests"]:
        data["pending_requests"].remove(uid)
    if uid not in data["admins"]:
        data["admins"].append(uid)
        save_data(data)
        return True
    save_data(data)
    return False


def remove_admin(uid):
    data = load_data()
    if uid in data["admins"]:
        data["admins"].remove(uid)
        save_data(data)
        return True
    return False


def add_request(uid):
    data = load_data()
    if uid in data["pending_requests"]:
        return False
    data["pending_requests"].append(uid)
    save_data(data)
    return True


def remove_request(uid):
    data = load_data()
    if uid in data["pending_requests"]:
        data["pending_requests"].remove(uid)
        save_data(data)


def ban_request(uid):
    data = load_data()
    if uid in data["pending_requests"]:
        data["pending_requests"].remove(uid)
    if uid not in data["banned_requests"]:
        data["banned_requests"].append(uid)
    save_data(data)


def is_req_banned(uid):
    return uid in load_data().get("banned_requests", [])


def toggle_sub(uid):
    data = load_data()
    if uid in data["subscribers"]:
        data["subscribers"].remove(uid)
        res = False
    else:
        data["subscribers"].append(uid)
        res = True
    save_data(data)
    return res


def remove_sub(uid):
    data = load_data()
    if uid in data["subscribers"]:
        data["subscribers"].remove(uid)
        save_data(data)