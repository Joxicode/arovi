import os
import re
import csv
import uuid
import asyncio
from pathlib import Path
from datetime import datetime, timezone

import flet as ft
from dotenv import load_dotenv
from supabase import create_client, Client


# ============================================================
# AROVI - ADULT MATCHMAKING MOBILE APPLICATION
# Flet 1.1.2 + Supabase Authentication + PostgreSQL
# Complete single-file application (Dedicated User & Admin Apps)
# ============================================================

APP_NAME = "AROVI"

PRIMARY = "#9B4D69"
DARK = "#321C2B"
ADMIN_DARK = "#1E121A"
GOLD = "#D6A56C"
BG = "#FFF8F5"
WHITE = "#FFFFFF"
MUTED = "#8A7880"
GREEN = "#267A59"
RED = "#B83232"
LIGHT_PINK = "#F3E2E9"
WARNING_BG = "#FFF3CD"
WARNING_TEXT = "#856404"


# ============================================================
# ENVIRONMENT CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()

SUPABASE_KEY = (
    os.getenv("SUPABASE_PUBLISHABLE_KEY")
    or os.getenv("SUPABASE_KEY")
    or ""
).strip()

SHOW_ALL = os.getenv("AROVI_SHOW_ALL", "0").strip() == "1"

supabase: Client | None = None
supabase_error = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_KEY
        )
    except Exception as exc:
        supabase_error = f"Supabase initialization failed: {exc}"
else:
    supabase_error = (
        "Supabase URL or publishable key is missing."
    )


def api():
    if supabase is None:
        raise RuntimeError(
            supabase_error or "Supabase is unavailable."
        )
    return supabase


# ============================================================
# SUPABASE DATABASE FUNCTIONS (USER SIDE)
# ============================================================

def get_profile(user_id):
    if not user_id:
        return None

    result = (
        api().table("profiles")
        .select("*")
        .eq("id", str(user_id))
        .limit(1)
        .execute()
    )

    row = result.data[0] if result.data else None

    if row:
        attach_avatars([row])

    return row


def get_auth_user():
    if supabase is None:
        return None

    try:
        result = supabase.auth.get_user()
        return result.user if result else None
    except Exception:
        return None


def norm(value):
    return (value or "").strip().lower()


def is_blocked(profile):
    """True when an admin has suspended or banned this account."""
    return norm((profile or {}).get("status")) in ("suspended", "banned")


def gender_label(gender):
    return {
        "man": "Men",
        "woman": "Women",
        "non-binary": "Non-binary",
    }.get(norm(gender), "")


def accepts(preference, other_gender):
    pref = norm(preference)

    if pref in ("", "everyone"):
        return True

    return pref == norm(gender_label(other_gender))


def preferences_match(first, second):
    return (
        accepts(first.get("interested_in"), second.get("gender"))
        and accepts(second.get("interested_in"), first.get("gender"))
    )


def get_blocked_ids(user_id):
    try:
        rows = (
            api().table("blocks")
            .select("blocker_id,blocked_id")
            .or_(f"blocker_id.eq.{user_id},blocked_id.eq.{user_id}")
            .execute()
            .data or []
        )
        blocked = set()
        for r in rows:
            if str(r["blocker_id"]) == str(user_id):
                blocked.add(str(r["blocked_id"]))
            else:
                blocked.add(str(r["blocker_id"]))
        return blocked
    except Exception:
        return set()


def block_user(blocker_id, blocked_id):
    api().table("blocks").upsert(
        {
            "blocker_id": str(blocker_id),
            "blocked_id": str(blocked_id),
        },
        on_conflict="blocker_id,blocked_id",
        ignore_duplicates=True,
    ).execute()

    one, two = sorted([str(blocker_id), str(blocked_id)])
    try:
        api().table("matches").delete().eq("user_one", one).eq("user_two", two).execute()
    except Exception:
        pass


def unmatch_users(user_one, user_two):
    one, two = sorted([str(user_one), str(user_two)])
    api().table("matches").delete().eq("user_one", one).eq("user_two", two).execute()


def get_discover_profiles(user_id, min_age=18, max_age=120, city_filter=""):
    me = get_profile(user_id)

    if not me:
        return []

    api_client = api()

    liked_rows = (
        api_client.table("likes")
        .select("receiver_id")
        .eq("sender_id", str(user_id))
        .execute()
        .data or []
    )
    liked_ids = {str(row["receiver_id"]) for row in liked_rows}

    match_rows = (
        api_client.table("matches")
        .select("user_one,user_two")
        .or_(f"user_one.eq.{user_id},user_two.eq.{user_id}")
        .execute()
        .data or []
    )

    matched_ids = set()
    for match in match_rows:
        other = (
            match["user_two"]
            if str(match["user_one"]) == str(user_id)
            else match["user_one"]
        )
        matched_ids.add(str(other))

    blocked_ids = get_blocked_ids(user_id)

    candidates = (
        api_client.table("profiles")
        .select("*")
        .or_("is_active.is.null,is_active.eq.true")
        .eq("status", "active")
        .neq("id", str(user_id))
        .order("created_at", desc=True)
        .execute()
        .data or []
    )

    city_q = norm(city_filter)
    results = []

    for person in candidates:
        person_id = str(person.get("id", ""))

        try:
            age = int(person.get("age") or 0)
        except (ValueError, TypeError):
            age = 0

        if not person_id or person_id in blocked_ids or age < 18:
            continue

        if age < min_age or age > max_age:
            continue

        if city_q and city_q not in norm(person.get("location")):
            continue

        if SHOW_ALL or preferences_match(me, person):
            person["is_locked_match"] = person_id in matched_ids
            person["already_liked"] = person_id in liked_ids
            results.append(person)

    results.sort(key=lambda p: (p["is_locked_match"], p["already_liked"]))
    attach_avatars(results)

    return results


def get_matches(user_id):
    blocked_ids = get_blocked_ids(user_id)

    rows = (
        api().table("matches")
        .select("*")
        .or_(f"user_one.eq.{user_id},user_two.eq.{user_id}")
        .order("created_at", desc=True)
        .execute()
        .data or []
    )

    people = []

    for match in rows:
        other_id = (
            match["user_two"]
            if str(match["user_one"]) == str(user_id)
            else match["user_one"]
        )

        if str(other_id) in blocked_ids:
            continue

        person = get_profile(other_id)

        if person and not is_blocked(person):
            person["match_id"] = match["id"]
            people.append(person)

    return people


def is_matched(user_one, user_two):
    one, two = sorted([str(user_one), str(user_two)])

    result = (
        api().table("matches")
        .select("id")
        .eq("user_one", one)
        .eq("user_two", two)
        .limit(1)
        .execute()
    )

    return bool(result.data)


def create_like(sender_id, receiver_id):
    sender_id = str(sender_id)
    receiver_id = str(receiver_id)

    if sender_id == receiver_id:
        return False

    receiver = get_profile(receiver_id)

    if (
        not receiver
        or receiver.get("is_active") is False
        or is_blocked(receiver)
    ):
        raise ValueError("This profile is unavailable.")

    api().table("likes").upsert(
        {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
        },
        on_conflict="sender_id,receiver_id",
        ignore_duplicates=True,
    ).execute()

    return is_matched(sender_id, receiver_id)


def get_conversation(user_one, user_two):
    return (
        api().table("messages")
        .select("*")
        .or_(
            f"and(sender_id.eq.{user_one},receiver_id.eq.{user_two}),"
            f"and(sender_id.eq.{user_two},receiver_id.eq.{user_one})"
        )
        .order("created_at")
        .execute()
        .data or []
    )


def get_last_message(user_one, user_two):
    rows = (
        api().table("messages")
        .select("message,sender_id,created_at")
        .or_(
            f"and(sender_id.eq.{user_one},receiver_id.eq.{user_two}),"
            f"and(sender_id.eq.{user_two},receiver_id.eq.{user_one})"
        )
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data or []
    )

    return rows[0] if rows else None


def save_message(sender_id, receiver_id, message):
    message = (message or "").strip()

    if not message or len(message) > 2000:
        return False

    if not is_matched(sender_id, receiver_id):
        return False

    api().table("messages").insert({
        "sender_id": str(sender_id),
        "receiver_id": str(receiver_id),
        "message": message,
    }).execute()

    return True


def get_active_announcement():
    try:
        rows = (
            api().table("announcements")
            .select("*")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data or []
        )
        return rows[0] if rows else None
    except Exception:
        return None


def dismiss_user_warning(user_id):
    try:
        api().table("profiles").update(
            {"admin_warning": None}
        ).eq("id", str(user_id)).execute()
    except Exception:
        pass


# ============================================================
# REPORTS (USER SIDE)
# ============================================================

REPORT_REASONS = [
    "Fake profile or scam",
    "Harassment or abusive messages",
    "Inappropriate photos",
    "Under 18",
    "Spam or advertising",
    "Other",
]


def submit_report(reporter_id, reported_id, reason):
    api().table("reports").insert({
        "reporter_id": str(reporter_id),
        "reported_id": str(reported_id),
        "reason": (reason or "").strip()[:1000],
    }).execute()


# ============================================================
# PROFILE PHOTOS (PRIVATE STORAGE BUCKET + SIGNED URLS)
# ============================================================

PHOTO_BUCKET = "profile-photos"
MAX_PHOTOS = 6
MAX_PHOTO_BYTES = 5 * 1024 * 1024
SIGNED_URL_SECONDS = 3600

IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}

_BOX_FIT = getattr(ft, "BoxFit", None) or getattr(ft, "ImageFit")
FIT_COVER = _BOX_FIT.COVER
FIT_CONTAIN = _BOX_FIT.CONTAIN

_url_cache = {}


def _full_url(url):
    if url and url.startswith("/"):
        return f"{SUPABASE_URL}/storage/v1{url}"
    return url


def signed_urls(paths):
    now = datetime.now(timezone.utc).timestamp()
    wanted = [p for p in dict.fromkeys(paths) if p]

    missing = [
        p for p in wanted
        if p not in _url_cache or _url_cache[p][1] < now + 300
    ]

    if missing:
        items = (
            api().storage.from_(PHOTO_BUCKET)
            .create_signed_urls(missing, SIGNED_URL_SECONDS)
        )

        for item in items or []:
            file_path = item.get("path")
            url = item.get("signedURL") or item.get("signedUrl")

            if file_path and url and not item.get("error"):
                _url_cache[file_path] = (
                    _full_url(url),
                    now + SIGNED_URL_SECONDS,
                )

    return {p: _url_cache[p][0] for p in wanted if p in _url_cache}


def attach_avatars(people):
    paths = [p.get("avatar_path") for p in people if p.get("avatar_path")]

    try:
        urls = signed_urls(paths) if paths else {}
    except Exception:
        urls = {}

    for person in people:
        person["avatar_url"] = urls.get(person.get("avatar_path"))

    return people


def get_photos(user_id):
    rows = (
        api().table("profile_photos")
        .select("*")
        .eq("user_id", str(user_id))
        .order("created_at")
        .execute()
        .data or []
    )

    if not rows:
        return []

    urls = signed_urls([r["path"] for r in rows])

    if not urls:
        raise RuntimeError(
            "Could not create photo links. Run arovi_photos.sql in "
            "Supabase and try again."
        )

    for row in rows:
        row["url"] = urls.get(row["path"])

    return [r for r in rows if r.get("url")]


def set_main_photo(user_id, file_path):
    api().table("profiles").update(
        {"avatar_path": file_path}
    ).eq("id", str(user_id)).execute()


def upload_photo(user_id, filename, data):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    content_type = IMAGE_TYPES.get(ext)

    if not content_type:
        raise ValueError("Use a JPG, PNG or WEBP image.")

    if not data or len(data) > MAX_PHOTO_BYTES:
        raise ValueError("Each photo must be 5 MB or smaller.")

    if len(get_photos(user_id)) >= MAX_PHOTOS:
        raise ValueError(f"You can upload up to {MAX_PHOTOS} photos.")

    file_path = f"{user_id}/{uuid.uuid4().hex}.{ext}"
    storage = api().storage.from_(PHOTO_BUCKET)

    storage.upload(file_path, data, {"content-type": content_type})

    try:
        api().table("profile_photos").insert({
            "user_id": str(user_id),
            "path": file_path,
        }).execute()
    except Exception:
        try:
            storage.remove([file_path])
        except Exception:
            pass
        raise

    profile = get_profile(user_id)

    if profile and not profile.get("avatar_path"):
        set_main_photo(user_id, file_path)

    return file_path


def delete_photo(user_id, photo):
    try:
        api().storage.from_(PHOTO_BUCKET).remove([photo["path"]])
    except Exception:
        pass

    (
        api().table("profile_photos")
        .delete()
        .eq("id", photo["id"])
        .eq("user_id", str(user_id))
        .execute()
    )

    _url_cache.pop(photo["path"], None)

    profile = get_profile(user_id)

    if profile and profile.get("avatar_path") == photo["path"]:
        remaining = get_photos(user_id)
        set_main_photo(
            user_id,
            remaining[0]["path"] if remaining else None,
        )


# ============================================================
# ADMIN DATABASE FUNCTIONS (SERVER-SIDE RPC PROTECTED)
# ============================================================

def check_admin(user_id):
    rows = (
        api().table("profiles")
        .select("is_admin")
        .eq("id", str(user_id))
        .limit(1)
        .execute()
        .data or []
    )

    return bool(rows and rows[0].get("is_admin"))


def _rpc(name, params=None):
    return api().rpc(name, params or {}).execute().data


def admin_stats():
    data = _rpc("admin_stats")
    return data if isinstance(data, dict) else {}


def admin_list_users(search="", filter_by="all", skip=0, limit=20):
    rows = _rpc("admin_list_users", {
        "search_text": (search or "").strip(),
        "filter_by": filter_by,
        "skip_rows": int(skip),
        "page_size": int(limit),
    }) or []

    attach_avatars(rows)

    return rows


def admin_user_summary(user_id):
    data = _rpc("admin_user_summary", {"target": str(user_id)})
    return data if isinstance(data, dict) else {}


def admin_user_matches(user_id):
    return _rpc("admin_user_matches", {"target": str(user_id)}) or []


def admin_unmatch(match_id):
    _rpc("admin_unmatch", {"match_id": str(match_id)})


def admin_set_status(user_id, status):
    _rpc("admin_set_status", {
        "target": str(user_id),
        "new_status": status,
    })


def admin_set_visibility(user_id, visible):
    _rpc("admin_set_visibility", {
        "target": str(user_id),
        "visible": bool(visible),
    })


def admin_set_admin(user_id, make_admin):
    _rpc("admin_set_admin", {
        "target": str(user_id),
        "make_admin": bool(make_admin),
    })


def admin_set_verified(user_id, verified):
    _rpc("admin_set_verified", {
        "target": str(user_id),
        "verified": bool(verified),
    })


def admin_send_warning(user_id, warning_text):
    _rpc("admin_send_warning", {
        "target": str(user_id),
        "warning_text": warning_text or "",
    })


def admin_update_full_profile(
    user_id, name, age, gender, interested_in, location, bio, status, is_active
):
    _rpc("admin_update_profile", {
        "target": str(user_id),
        "new_name": name,
        "new_age": int(age),
        "new_gender": gender,
        "new_interested_in": interested_in,
        "new_location": location,
        "new_bio": bio,
        "new_status": status,
        "new_is_active": bool(is_active),
    })


def admin_clear_bio(user_id):
    _rpc("admin_clear_bio", {"target": str(user_id)})


def admin_delete_user(user_id):
    photo_rows = (
        api().table("profile_photos")
        .select("path")
        .eq("user_id", str(user_id))
        .execute()
        .data or []
    )

    paths = [r["path"] for r in photo_rows if r.get("path")]

    _rpc("admin_delete_user", {"target": str(user_id)})

    if paths:
        try:
            api().storage.from_(PHOTO_BUCKET).remove(paths)
        except Exception:
            pass


def admin_recent_photos(skip=0, limit=12):
    return _rpc("admin_recent_photos", {
        "skip_rows": int(skip),
        "page_size": int(limit),
    }) or []


def admin_remove_photo(photo):
    _rpc("admin_remove_photo", {"photo_id": str(photo["id"])})

    try:
        api().storage.from_(PHOTO_BUCKET).remove([photo["path"]])
    except Exception:
        pass

    _url_cache.pop(photo["path"], None)


def admin_list_reports(filter_by="open"):
    return _rpc("admin_list_reports", {"filter_by": filter_by}) or []


def admin_resolve_report(report_id, status):
    _rpc("admin_resolve_report", {
        "rid": str(report_id),
        "new_status": status,
    })


def admin_get_conversation(first_id, second_id):
    return _rpc("admin_get_conversation", {
        "a": str(first_id),
        "b": str(second_id),
    }) or []


def admin_list_announcements():
    try:
        return (
            api().table("announcements")
            .select("*")
            .order("created_at", desc=True)
            .limit(15)
            .execute()
            .data or []
        )
    except Exception:
        return []


def admin_publish_announcement(title, body):
    api().table("announcements").update({"is_active": False}).eq("is_active", True).execute()
    if title.strip() and body.strip():
        api().table("announcements").insert({
            "title": title.strip()[:120],
            "body": body.strip()[:500],
            "is_active": True,
        }).execute()


def admin_toggle_announcement(ann_id, make_active):
    if make_active:
        api().table("announcements").update({"is_active": False}).eq("is_active", True).execute()
    api().table("announcements").update({"is_active": bool(make_active)}).eq("id", str(ann_id)).execute()


def admin_delete_announcement(ann_id):
    api().table("announcements").delete().eq("id", str(ann_id)).execute()


# ============================================================
# FLET APPLICATION
# ============================================================

def main(page: ft.Page):

    page.title = "AROVI - Meaningful Connections"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = BG
    page.padding = 0
    page.spacing = 0

    file_picker = ft.FilePicker()

    try:
        page.services.append(file_picker)
    except AttributeError:
        page.overlay.append(file_picker)

    auth_user = get_auth_user()
    current_user_id = auth_user.id if auth_user else None

    current_tab = "Discover"
    active_notice = None

    state = {
        "queue": None,
        "index": 0,
        "chat_token": 0,
        "user": None,
        "min_age": 18,
        "max_age": 99,
        "city_filter": "",
        "admin_mode": False,
        "admin": False,
        "atab": "Dashboard",
        "asearch": "",
        "afilter": "all",
        "apage": 0,
        "aphoto_page": 0,
        "areport": "open",
    }

    # ========================================================
    # REUSABLE UI COMPONENTS
    # ========================================================

    def make_notice():
        nonlocal active_notice

        active_notice = ft.Text(
            "",
            size=12,
            color=PRIMARY,
            text_align=ft.TextAlign.CENTER,
        )

        return active_notice

    def notify(message, color=PRIMARY):
        if active_notice is not None:
            active_notice.value = str(message)
            active_notice.color = color

            try:
                page.update()
            except Exception:
                pass

    def field(label, password=False, keyboard=None, multiline=False):
        return ft.TextField(
            label=label,
            password=password,
            can_reveal_password=password,
            keyboard_type=keyboard,
            multiline=multiline,
            filled=True,
            bgcolor=WHITE,
            text_size=14,
            border_radius=12,
        )

    def primary_button(label, action, bgcolor=PRIMARY):
        return ft.Row(
            controls=[
                ft.Button(
                    content=label,
                    on_click=action,
                    bgcolor=bgcolor,
                    color=WHITE,
                    height=48,
                    expand=True,
                )
            ]
        )

    def secondary_button(label, action):
        return ft.OutlinedButton(
            content=label,
            on_click=action,
        )

    def wide_button(label, action, bgcolor=PRIMARY):
        return ft.Row(
            controls=[
                ft.Button(
                    content=label,
                    on_click=action,
                    bgcolor=bgcolor,
                    color=WHITE,
                    height=48,
                    expand=True,
                )
            ]
        )

    def heading(title, subtitle=None):
        controls = [
            ft.Text(
                title,
                size=24,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            )
        ]

        if subtitle:
            controls.append(
                ft.Text(
                    subtitle,
                    size=13,
                    color=MUTED,
                )
            )

        return ft.Column(
            controls=controls,
            spacing=4,
        )

    def panel(content, padding=16):
        return ft.Container(
            content=content,
            padding=padding,
            bgcolor=WHITE,
            border_radius=18,
        )

    def app_logo(size=30):
        return ft.Row(
            main_size=ft.MainAxisSize.MIN,
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(
                    ft.Icons.FAVORITE,
                    size=max(18, int(size * 0.85)),
                    color=PRIMARY,
                ),
                ft.Text(
                    APP_NAME,
                    size=size,
                    weight=ft.FontWeight.BOLD,
                    color=PRIMARY,
                ),
            ],
        )

    def centered_icon(icon, size=48):
        return ft.Container(
            width=90,
            height=90,
            bgcolor=LIGHT_PINK,
            border_radius=25,
            alignment=ft.Alignment.CENTER,
            content=ft.Icon(
                icon,
                size=size,
                color=PRIMARY,
            ),
        )

    def verified_badge(person):
        if not (person or {}).get("is_verified"):
            return ft.Container(width=0, height=0)
        return ft.Icon(
            ft.Icons.VERIFIED,
            color="#1D9BF0",
            size=18,
            tooltip="Verified Member",
        )

    def person_avatar(size=54, person=None):
        url = (person or {}).get("avatar_url")

        if url:
            return ft.Container(
                width=size,
                height=size,
                bgcolor=LIGHT_PINK,
                border_radius=size / 2,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=ft.Image(
                    src=url,
                    width=size,
                    height=size,
                    fit=FIT_COVER,
                ),
            )

        return ft.Container(
            width=size,
            height=size,
            bgcolor=LIGHT_PINK,
            border_radius=size / 2,
            alignment=ft.Alignment.CENTER,
            content=ft.Icon(
                ft.Icons.PERSON,
                size=size * 0.55,
                color=PRIMARY,
            ),
        )

    def show_photo_viewer(urls, index=0):
        index = max(0, min(index, len(urls) - 1))

        def move(step):
            show_photo_viewer(urls, (index + step) % len(urls))

        page.clean()
        page.bgcolor = "#000000"
        page.padding = 0

        page.add(
            ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    ft.Container(
                        padding=10,
                        content=ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            controls=[
                                ft.Text(
                                    f"{index + 1} / {len(urls)}",
                                    color=WHITE,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CLOSE,
                                    icon_color=WHITE,
                                    on_click=lambda e: (
                                        open_admin()
                                        if state.get("admin")
                                        else open_dashboard()
                                    ),
                                ),
                            ],
                        ),
                    ),
                    ft.Container(
                        expand=True,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Image(
                            src=urls[index],
                            fit=FIT_CONTAIN,
                            expand=True,
                        ),
                    ),
                    ft.Container(
                        padding=10,
                        content=ft.Row(
                            alignment=ft.MainAxisAlignment.CENTER,
                            spacing=40,
                            controls=[
                                ft.IconButton(
                                    icon=ft.Icons.CHEVRON_LEFT,
                                    icon_color=WHITE,
                                    icon_size=36,
                                    on_click=lambda e: move(-1),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CHEVRON_RIGHT,
                                    icon_color=WHITE,
                                    icon_size=36,
                                    on_click=lambda e: move(1),
                                ),
                            ],
                        ),
                    ),
                ],
            )
        )

        page.update()

    def gallery_view(photos, person=None, height=320):
        if not photos:
            return person_avatar(120, person)

        urls = [p["url"] for p in photos]
        current = {"url": urls[0]}

        big = ft.Image(src=urls[0], fit=FIT_COVER, expand=True)

        thumbs = ft.Row(
            spacing=8,
            wrap=True,
            alignment=ft.MainAxisAlignment.CENTER,
        )

        def build_thumbs(active):
            thumbs.controls = [
                ft.Container(
                    width=54,
                    height=54,
                    border_radius=10,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    opacity=1 if u == active else 0.5,
                    on_click=lambda e, u=u: select(u),
                    content=ft.Image(
                        src=u,
                        width=54,
                        height=54,
                        fit=FIT_COVER,
                    ),
                )
                for u in urls
            ]

        def select(url):
            current["url"] = url
            big.src = url
            build_thumbs(url)
            page.update()

        build_thumbs(urls[0])

        column_controls = [
            ft.Container(
                height=height,
                border_radius=16,
                bgcolor=LIGHT_PINK,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=big,
                on_click=lambda e: show_photo_viewer(
                    urls,
                    urls.index(current["url"]),
                ),
            )
        ]

        if len(urls) > 1:
            column_controls.append(thumbs)

        return ft.Row(
            controls=[
                ft.Container(
                    expand=True,
                    content=ft.Column(
                        spacing=10,
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.STRETCH
                        ),
                        controls=column_controls,
                    ),
                )
            ]
        )

    def make_scrollable(controls):
        return ft.Column(
            controls=controls,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=14,
        )

    def display_name(person):
        return person.get("name") or "AROVI member"

    def person_subtitle(person):
        parts = []

        if person.get("age"):
            parts.append(str(person["age"]))

        if person.get("location"):
            parts.append(person["location"])

        return " \u2022 ".join(parts)

    def person_row(person, subtitle, on_tap, trailing=None):
        row_controls = [
            person_avatar(52, person),
            ft.Column(
                expand=True,
                spacing=2,
                controls=[
                    ft.Row(
                        spacing=4,
                        controls=[
                            ft.Text(
                                display_name(person),
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            verified_badge(person),
                        ],
                    ),
                    ft.Text(
                        subtitle,
                        size=12,
                        color=MUTED,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
            ),
        ]

        if trailing:
            row_controls.append(trailing)
        else:
            row_controls.append(ft.Icon(ft.Icons.CHEVRON_RIGHT, color=MUTED))

        return ft.Container(
            bgcolor=WHITE,
            border_radius=18,
            padding=12,
            on_click=on_tap,
            content=ft.Row(
                spacing=12,
                controls=row_controls,
            ),
        )

    def empty_view(icon, title, subtitle):
        return ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    centered_icon(icon),
                    ft.Text(
                        title,
                        size=18,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        subtitle,
                        size=13,
                        color=MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
            ),
        )

    def error_view(title, exc, retry):
        return make_scrollable([
            app_logo(25),
            heading(title, "Something went wrong talking to the server."),
            panel(
                ft.Text(
                    str(exc),
                    size=12,
                    color=MUTED,
                    selectable=True,
                )
            ),
            wide_button("TRY AGAIN", lambda e: retry()),
        ])

    def show_screen(content, navigation=False, nav=None, bg=BG):
        page.clean()
        page.bgcolor = bg
        page.padding = 0

        items = [
            ft.Container(
                content=content,
                padding=16,
                expand=True,
            )
        ]

        if nav is not None:
            items.append(nav)
        elif navigation and current_user_id and not state.get("admin"):
            items.append(build_bottom_navigation())

        page.add(
            ft.Column(
                controls=items,
                expand=True,
                spacing=0,
            )
        )

        page.update()

    # ========================================================
    # NAVIGATION BUILDERS (SEPARATE USER VS ADMIN BARS)
    # ========================================================

    def set_tab(tab):
        nonlocal current_tab

        current_tab = tab
        open_dashboard()

    def build_nav(tabs, selected_name, on_select, bar_bg=WHITE, active_color=PRIMARY, inactive_color=MUTED):
        buttons = []

        for tab_name, tab_icon in tabs:
            selected = selected_name == tab_name

            buttons.append(
                ft.Container(
                    expand=True,
                    content=ft.TextButton(
                        content=ft.Column(
                            controls=[
                                ft.Icon(
                                    tab_icon,
                                    color=active_color if selected else inactive_color,
                                    size=20,
                                ),
                                ft.Text(
                                    tab_name,
                                    size=10,
                                    weight=ft.FontWeight.BOLD if selected else ft.FontWeight.NORMAL,
                                    color=active_color if selected else inactive_color,
                                ),
                            ],
                            horizontal_alignment=(
                                ft.CrossAxisAlignment.CENTER
                            ),
                            spacing=2,
                        ),
                        on_click=lambda e, t=tab_name: on_select(t),
                    ),
                )
            )

        return ft.Container(
            bgcolor=bar_bg,
            padding=6,
            content=ft.Row(
                controls=buttons,
                alignment=ft.MainAxisAlignment.SPACE_AROUND,
            ),
        )

    def build_bottom_navigation():
        return build_nav(
            [
                ("Discover", ft.Icons.EXPLORE_OUTLINED),
                ("Matches", ft.Icons.FAVORITE_BORDER),
                ("Messages", ft.Icons.CHAT_BUBBLE_OUTLINE),
                ("Profile", ft.Icons.PERSON_OUTLINE),
            ],
            current_tab,
            set_tab,
        )

    # ========================================================
    # SPLASH SCREEN (RESTORED AROVI SPLASH, NO FLET LOGO)
    # ========================================================

    def show_splash():
        page.clean()
        page.bgcolor = DARK
        page.padding = 0

        page.add(
            ft.Container(
                expand=True,
                bgcolor=DARK,
                alignment=ft.Alignment.CENTER,
                content=ft.Column(
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=(
                        ft.CrossAxisAlignment.CENTER
                    ),
                    spacing=18,
                    controls=[
                        ft.Container(
                            width=105,
                            height=105,
                            border_radius=30,
                            bgcolor=PRIMARY,
                            alignment=ft.Alignment.CENTER,
                            content=ft.Icon(
                                ft.Icons.FAVORITE,
                                color=WHITE,
                                size=55,
                            ),
                        ),
                        ft.Text(
                            "AROVI",
                            size=42,
                            weight=ft.FontWeight.BOLD,
                            color=WHITE,
                        ),
                        ft.Text(
                            "MEANINGFUL CONNECTIONS",
                            size=11,
                            color=GOLD,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "Meet someone who matters.",
                            size=15,
                            color="#F6E9EE",
                        ),
                        ft.ProgressRing(
                            width=25,
                            height=25,
                            color=GOLD,
                        ),
                    ],
                ),
            )
        )

        page.update()

        async def continue_after_splash():
            await asyncio.sleep(1.5)

            if current_user_id:
                open_dashboard()
            else:
                show_welcome()

        page.run_task(continue_after_splash)

    # ========================================================
    # WELCOME SCREEN
    # ========================================================

    def show_welcome():
        notice = make_notice()

        content = ft.Column(
            expand=True,
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=17,
            controls=[
                centered_icon(ft.Icons.FAVORITE, 52),
                app_logo(38),
                ft.Text(
                    "A better way to find your person.",
                    size=16,
                    color=DARK,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Text(
                    "A community for adults seeking genuine "
                    "connections and serious relationships.",
                    size=13,
                    color=MUTED,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Container(height=12),
                notice,
                primary_button(
                    "CREATE ACCOUNT",
                    show_register,
                ),
                secondary_button(
                    "I ALREADY HAVE AN ACCOUNT",
                    show_login,
                ),
                ft.Text(
                    "18+ ONLY \u2022 RESPECT \u2022 HONEST CONNECTIONS",
                    size=10,
                    color=MUTED,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
        )

        show_screen(content)

    # ========================================================
    # REGISTRATION (Always redirects to Login screen on success)
    # ========================================================

    def show_register(e=None):
        notice = make_notice()

        name = field("Full name")

        email = field(
            "Email address",
            keyboard=ft.KeyboardType.EMAIL,
        )

        age = field(
            "Age",
            keyboard=ft.KeyboardType.NUMBER,
        )

        gender = ft.Dropdown(
            label="I identify as",
            options=[
                ft.dropdown.Option("Man"),
                ft.dropdown.Option("Woman"),
                ft.dropdown.Option("Non-binary"),
            ],
        )

        interested = ft.Dropdown(
            label="I am interested in",
            options=[
                ft.dropdown.Option("Men"),
                ft.dropdown.Option("Women"),
                ft.dropdown.Option("Non-binary"),
                ft.dropdown.Option("Everyone"),
            ],
        )

        location = field("City / Location")

        password = field(
            "Create password",
            password=True,
        )

        confirm = field(
            "Confirm password",
            password=True,
        )

        terms = ft.Checkbox(
            label=(
                "I confirm that I am 18 or older and agree "
                "to use AROVI respectfully."
            )
        )

        def register(e):
            nonlocal current_user_id

            full_name = (name.value or "").strip()
            mail = (email.value or "").strip().lower()
            place = (location.value or "").strip()

            secret = password.value or ""
            confirmation = confirm.value or ""

            try:
                age_value = int(age.value or "0")
            except ValueError:
                notify("Enter a valid age.")
                return

            if not full_name or not mail or not place:
                notify("Please complete all required fields.")
                return

            if not re.match(
                r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                mail,
            ):
                notify("Enter a valid email address.")
                return

            if age_value < 18 or age_value > 120:
                notify("AROVI is only for adults aged 18 and above.")
                return

            if not gender.value or not interested.value:
                notify("Select your gender and preference.")
                return

            if len(secret) < 8:
                notify("Password must be at least 8 characters.")
                return

            if secret != confirmation:
                notify("Your passwords do not match.")
                return

            if not terms.value:
                notify("Please confirm the 18+ requirement.")
                return

            try:
                result = api().auth.sign_up({
                    "email": mail,
                    "password": secret,
                    "options": {
                        "data": {
                            "name": full_name,
                            "age": age_value,
                            "gender": gender.value,
                            "interested_in": interested.value,
                            "location": place,
                        }
                    },
                })

                if result.user is None:
                    notify("Registration could not be completed.")
                    return

                if result.session:
                    try:
                        api().auth.sign_out()
                    except Exception:
                        pass

                current_user_id = None
                show_login()
                notify(
                    "Account created! Please sign in with your credentials.",
                    GREEN,
                )

            except Exception as ex:
                notify(
                    f"Registration error: {type(ex).__name__}: {str(ex)}"
                )

        content = make_scrollable([
            app_logo(30),
            heading(
                "Create your account",
                "Join AROVI and meet people who share your intentions.",
            ),
            name,
            email,
            age,
            gender,
            interested,
            location,
            password,
            confirm,
            terms,
            notice,
            primary_button(
                "CREATE ACCOUNT",
                register,
            ),
            secondary_button(
                "Already registered? Sign in",
                show_login,
            ),
        ])

        show_screen(content)

    # ========================================================
    # LOGIN & PASSWORD RESET
    # ========================================================

    def show_forgot_password(e=None):
        notice = make_notice()
        email = field("Registered email address", keyboard=ft.KeyboardType.EMAIL)

        def send_reset(e):
            mail = (email.value or "").strip().lower()
            if not mail:
                notify("Enter your email address.")
                return
            try:
                api().auth.reset_password_for_email(mail)
                notify(
                    "If an account exists for that email, a reset link was sent.",
                    GREEN,
                )
            except Exception as ex:
                notify(f"Could not send reset email: {ex}")

        show_screen(
            make_scrollable([
                app_logo(30),
                heading(
                    "Reset password",
                    "Enter your email to receive password reset instructions.",
                ),
                email,
                notice,
                wide_button("SEND RESET LINK", send_reset),
                secondary_button("BACK TO SIGN IN", show_login),
            ])
        )

    def show_login(e=None):
        notice = make_notice()

        state["admin_mode"] = False
        state["admin"] = False
        taps = {"count": 0}

        admin_tag = ft.Container(
            bgcolor=ADMIN_DARK,
            border_radius=8,
            padding=ft.Padding(left=10, top=4, right=10, bottom=4),
            visible=False,
            content=ft.Text(
                "ADMIN CONSOLE ACCESS",
                size=11,
                weight=ft.FontWeight.BOLD,
                color=GOLD,
            ),
        )

        def logo_tap(e):
            taps["count"] += 1

            if taps["count"] >= 3:
                taps["count"] = 0
                state["admin_mode"] = True
                admin_tag.visible = True
                page.update()

        email = field(
            "Email address",
            keyboard=ft.KeyboardType.EMAIL,
        )

        password = field(
            "Password",
            password=True,
        )

        def login(e):
            nonlocal current_user_id, current_tab

            mail = (email.value or "").strip().lower()
            secret = password.value or ""

            if not mail or not secret:
                notify("Enter your email and password.")
                return

            try:
                result = api().auth.sign_in_with_password({
                    "email": mail,
                    "password": secret,
                })

                if not result.user:
                    notify("Incorrect email address or password.")
                    return

                current_user_id = result.user.id
                profile = get_profile(current_user_id)

                if not profile or int(profile.get("age") or 0) < 18:
                    api().auth.sign_out()
                    current_user_id = None
                    notify(
                        "This account has no valid profile yet. "
                        "Run the Supabase setup SQL, then sign in again."
                    )
                    return

                if is_blocked(profile):
                    api().auth.sign_out()
                    current_user_id = None
                    notify(
                        "This account has been suspended or banned. "
                        "Contact AROVI support if you think this is a mistake."
                    )
                    return

                if state.get("admin_mode") or profile.get("is_admin"):
                    if not profile.get("is_admin"):
                        api().auth.sign_out()
                        current_user_id = None
                        state["admin_mode"] = False
                        notify("This account is not authorized for Admin Console.")
                        return

                    state["admin"] = True
                    state["atab"] = "Dashboard"
                    open_admin()
                    return

                state["admin"] = False
                current_tab = "Discover"
                open_dashboard()

            except Exception as ex:
                notify(f"Sign-in failed: {ex}")

        content = ft.Column(
            expand=True,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=15,
            controls=[
                ft.Container(
                    content=app_logo(34),
                    on_click=logo_tap,
                ),
                admin_tag,
                heading(
                    "Welcome back",
                    "Sign in to continue your journey.",
                ),
                email,
                password,
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        ft.TextButton(
                            content="Forgot password?",
                            on_click=show_forgot_password,
                        )
                    ],
                ),
                notice,
                primary_button("SIGN IN", login),
                secondary_button(
                    "Create a new account",
                    show_register,
                ),
            ],
        )

        show_screen(content)

    # ========================================================
    # DASHBOARD ROUTER (STRICTLY SEPARATES USER & ADMIN)
    # ========================================================

    def open_dashboard():
        nonlocal current_user_id

        state["chat_token"] += 1

        if not current_user_id:
            show_welcome()
            return

        if state.get("admin"):
            open_admin()
            return

        if state["user"] != current_user_id:
            state.update(queue=None, index=0, user=current_user_id)

        try:
            user = get_profile(current_user_id)
        except Exception as ex:
            show_screen(
                error_view(
                    "Connection problem",
                    ex,
                    open_dashboard,
                )
            )
            return

        if not user or int(user.get("age") or 0) < 18:
            try:
                api().auth.sign_out()
            except Exception:
                pass
            current_user_id = None
            show_welcome()
            return

        if is_blocked(user):
            try:
                api().auth.sign_out()
            except Exception:
                pass
            current_user_id = None
            state.update(queue=None, index=0, user=None)
            show_welcome()
            notify(
                "This account has been suspended or banned. "
                "Contact AROVI support if you think this is a mistake."
            )
            return

        if current_tab == "Discover":
            show_discover()
        elif current_tab == "Matches":
            show_matches()
        elif current_tab == "Messages":
            show_messages()
        elif current_tab == "Profile":
            show_profile()
        else:
            show_discover()

    # ========================================================
    # USER SIDE: REPORT & BLOCK A MEMBER
    # ========================================================

    def show_report(person_id, person_name, back):
        notice = make_notice()

        reason = ft.Dropdown(
            label="Reason",
            options=[ft.dropdown.Option(r) for r in REPORT_REASONS],
        )

        details = ft.TextField(
            label="Details (optional)",
            multiline=True,
            min_lines=3,
            max_lines=5,
            filled=True,
            bgcolor=WHITE,
            border_radius=12,
        )

        also_block = ft.Checkbox(
            label=f"Also block {person_name} immediately",
            value=True,
        )

        def submit(e):
            if not reason.value:
                notify("Choose a reason first.")
                return

            extra = (details.value or "").strip()
            text = reason.value + (f": {extra}" if extra else "")

            try:
                submit_report(current_user_id, person_id, text)
                if also_block.value:
                    block_user(current_user_id, person_id)
                    state["queue"] = None
            except Exception as ex:
                notify(f"Could not send report: {ex}")
                return

            back()
            notify(
                "Report submitted. Our team will review it.",
                GREEN,
            )

        show_screen(
            make_scrollable([
                app_logo(25),
                heading(
                    f"Report {person_name}",
                    "Tell us what is wrong. Reports are reviewed "
                    "by the AROVI team.",
                ),
                reason,
                details,
                also_block,
                notice,
                wide_button("SEND REPORT", submit),
                secondary_button("CANCEL", lambda e: back()),
            ])
        )

    # ========================================================
    # USER SIDE: DISCOVER (AVAILABLE USERS + LOCKED MATCHES)
    # ========================================================

    def reload_discover(e=None):
        state["queue"] = None
        state["index"] = 0
        show_discover()

    def show_filter_modal(e=None):
        min_f = field("Minimum Age", keyboard=ft.KeyboardType.NUMBER)
        min_f.value = str(state.get("min_age", 18))

        max_f = field("Maximum Age", keyboard=ft.KeyboardType.NUMBER)
        max_f.value = str(state.get("max_age", 99))

        city_f = field("City / Location (leave empty for all)")
        city_f.value = state.get("city_filter", "")

        notice = make_notice()

        def apply_filters(e):
            try:
                lo = max(18, int(min_f.value or 18))
                hi = min(120, int(max_f.value or 99))
                if lo > hi:
                    notify("Minimum age cannot exceed maximum age.")
                    return
            except ValueError:
                notify("Enter valid age numbers.")
                return

            state["min_age"] = lo
            state["max_age"] = hi
            state["city_filter"] = (city_f.value or "").strip()
            reload_discover()

        def reset_filters(e):
            state["min_age"] = 18
            state["max_age"] = 99
            state["city_filter"] = ""
            reload_discover()

        show_screen(
            make_scrollable([
                app_logo(25),
                heading("Discovery Filters", "Refine who appears in your feed."),
                min_f,
                max_f,
                city_f,
                notice,
                wide_button("APPLY FILTERS", apply_filters),
                secondary_button("RESET FILTERS", reset_filters),
                secondary_button("CANCEL", lambda e: show_discover()),
            ]),
            navigation=True,
        )

    def show_match_screen(person):
        name = display_name(person)

        show_screen(
            ft.Column(
                expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=16,
                controls=[
                    centered_icon(ft.Icons.FAVORITE, 52),
                    ft.Text(
                        "It's a match!",
                        size=30,
                        weight=ft.FontWeight.BOLD,
                        color=PRIMARY,
                    ),
                    ft.Text(
                        f"You and {name} liked each other. This match is now locked in.",
                        size=14,
                        color=MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Container(height=8),
                    wide_button(
                        "SEND A MESSAGE",
                        lambda e: open_chat(person["id"], "Matches"),
                    ),
                    secondary_button(
                        "KEEP BROWSING",
                        lambda e: reload_discover(),
                    ),
                ],
            )
        )

    def show_discover():
        try:
            user = get_profile(current_user_id)

            if state["queue"] is None:
                state["queue"] = get_discover_profiles(
                    current_user_id,
                    min_age=state["min_age"],
                    max_age=state["max_age"],
                    city_filter=state["city_filter"],
                )
                state["index"] = 0

        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load Discover",
                    ex,
                    reload_discover,
                ),
                navigation=True,
            )
            return

        notice = make_notice()

        name_parts = (user.get("name") or "").split()
        first_name = name_parts[0] if name_parts else "there"

        header = ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                app_logo(25),
                ft.Row(
                    spacing=0,
                    controls=[
                        ft.Text(
                            f"Hello, {first_name}",
                            color=DARK,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.TUNE,
                            icon_color=PRIMARY,
                            tooltip="Filter",
                            on_click=show_filter_modal,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.REFRESH,
                            icon_color=PRIMARY,
                            tooltip="Refresh",
                            on_click=reload_discover,
                        ),
                    ],
                ),
            ],
        )

        top_banners = []

        if user.get("admin_warning"):
            top_banners.append(
                ft.Container(
                    bgcolor=WARNING_BG,
                    border_radius=12,
                    padding=12,
                    content=ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Column(
                                expand=True,
                                spacing=2,
                                controls=[
                                    ft.Text(
                                        "Notice from AROVI Safety Team",
                                        weight=ft.FontWeight.BOLD,
                                        color=WARNING_TEXT,
                                        size=12,
                                    ),
                                    ft.Text(
                                        user["admin_warning"],
                                        color=WARNING_TEXT,
                                        size=12,
                                    ),
                                ],
                            ),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_size=18,
                                icon_color=WARNING_TEXT,
                                on_click=lambda e: (
                                    dismiss_user_warning(current_user_id),
                                    show_discover(),
                                ),
                            ),
                        ],
                    ),
                )
            )

        announcement = get_active_announcement()
        if announcement:
            top_banners.append(
                ft.Container(
                    bgcolor=LIGHT_PINK,
                    border_radius=12,
                    padding=12,
                    content=ft.Column(
                        spacing=2,
                        controls=[
                            ft.Row(
                                spacing=6,
                                controls=[
                                    ft.Icon(ft.Icons.CAMPAIGN, color=PRIMARY, size=18),
                                    ft.Text(
                                        announcement.get("title", "Announcement"),
                                        weight=ft.FontWeight.BOLD,
                                        color=DARK,
                                        size=13,
                                    ),
                                ],
                            ),
                            ft.Text(
                                announcement.get("body", ""),
                                color=DARK,
                                size=12,
                            ),
                        ],
                    ),
                )
            )

        if not user.get("gender") or not user.get("interested_in"):
            show_screen(
                ft.Column(
                    expand=True,
                    spacing=14,
                    controls=[
                        header,
                        *top_banners,
                        empty_view(
                            ft.Icons.EDIT_OUTLINED,
                            "Finish your profile",
                            "Set who you are and who you're interested in "
                            "on the Profile tab so we can show you people.",
                        ),
                        wide_button(
                            "GO TO PROFILE",
                            lambda e: set_tab("Profile"),
                        ),
                    ],
                ),
                navigation=True,
            )
            return

        queue = state["queue"]
        idx = state["index"]

        if idx >= len(queue):
            show_screen(
                ft.Column(
                    expand=True,
                    spacing=14,
                    controls=[
                        header,
                        *top_banners,
                        empty_view(
                            ft.Icons.EXPLORE_OUTLINED,
                            "No profiles match right now",
                            "Try adjusting your filters or check back later.",
                        ),
                        wide_button("REFRESH", reload_discover),
                    ],
                ),
                navigation=True,
            )
            return

        person = queue[idx]
        locked_match = bool(person.get("is_locked_match"))
        already_liked = bool(person.get("already_liked"))

        try:
            person_photos = get_photos(person["id"])
        except Exception:
            person_photos = []

        def like_person(e):
            if locked_match:
                return
            try:
                mutual = create_like(
                    current_user_id,
                    person["id"],
                )
            except Exception as ex:
                notify(f"Could not send like: {ex}")
                return

            if mutual:
                state["queue"] = None
                show_match_screen(person)
            else:
                person["already_liked"] = True
                state["index"] = (idx + 1) % len(queue)
                show_discover()
                notify(
                    "Like sent. If they like you back, you'll match.",
                    GREEN,
                )

        def next_person(e):
            state["index"] = (idx + 1) % len(queue)
            show_discover()

        def prev_person(e):
            state["index"] = (idx - 1) % len(queue)
            show_discover()

        def after_report():
            state["queue"] = None
            show_discover()

        status_badges = []
        if locked_match:
            status_badges.append(
                ft.Container(
                    bgcolor=GREEN,
                    border_radius=20,
                    padding=ft.Padding(left=12, top=5, right=12, bottom=5),
                    content=ft.Row(
                        main_size=ft.MainAxisSize.MIN,
                        spacing=4,
                        controls=[
                            ft.Icon(ft.Icons.LOCK, color=WHITE, size=14),
                            ft.Text(
                                "MATCHED & LOCKED",
                                color=WHITE,
                                size=11,
                                weight=ft.FontWeight.BOLD,
                            ),
                        ],
                    ),
                )
            )
        elif already_liked:
            status_badges.append(
                ft.Container(
                    bgcolor=GOLD,
                    border_radius=20,
                    padding=ft.Padding(left=12, top=5, right=12, bottom=5),
                    content=ft.Text(
                        "LIKE SENT \u2022 AWAITING REPLY",
                        color=DARK,
                        size=11,
                        weight=ft.FontWeight.BOLD,
                    ),
                )
            )

        card_controls = [
            *status_badges,
            gallery_view(person_photos, person),
            ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=6,
                controls=[
                    ft.Text(
                        f"{display_name(person)}, {person.get('age', '')}",
                        size=24,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    verified_badge(person),
                ],
            ),
            ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=4,
                controls=[
                    ft.Icon(
                        ft.Icons.LOCATION_ON_OUTLINED,
                        size=16,
                        color=PRIMARY,
                    ),
                    ft.Text(
                        person.get("location") or "Location not provided",
                        size=13,
                        color=MUTED,
                    ),
                ],
            ),
        ]

        bio_text = (person.get("bio") or "").strip()

        card_controls.append(
            ft.Text(
                bio_text or "This member has not added a bio yet.",
                size=14,
                color=DARK if bio_text else MUTED,
                text_align=ft.TextAlign.CENTER,
            )
        )

        if locked_match:
            actions = ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10,
                controls=[
                    wide_button(
                        f"MESSAGE {display_name(person).upper()}",
                        lambda e: open_chat(person["id"], "Discover"),
                    ),
                    ft.Row(
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=16,
                        controls=[
                            secondary_button("< PREVIOUS", prev_person),
                            secondary_button("NEXT PROFILE >", next_person),
                        ],
                    ),
                ],
            )
        else:
            actions = ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=22,
                controls=[
                    ft.Container(
                        bgcolor=WHITE,
                        border_radius=40,
                        content=ft.IconButton(
                            icon=ft.Icons.ARROW_BACK,
                            icon_size=24,
                            icon_color=MUTED,
                            tooltip="Previous",
                            on_click=prev_person,
                        ),
                    ),
                    ft.Container(
                        bgcolor=WHITE,
                        border_radius=40,
                        content=ft.IconButton(
                            icon=ft.Icons.CLOSE,
                            icon_size=32,
                            icon_color=MUTED,
                            tooltip="Skip",
                            on_click=next_person,
                        ),
                    ),
                    ft.Container(
                        bgcolor=GOLD if already_liked else PRIMARY,
                        border_radius=40,
                        content=ft.IconButton(
                            icon=ft.Icons.FAVORITE,
                            icon_size=32,
                            icon_color=WHITE,
                            tooltip="Already Liked" if already_liked else "Like",
                            on_click=like_person,
                        ),
                    ),
                ],
            )

        report_row = ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            controls=[
                ft.TextButton(
                    content="Report or Block",
                    on_click=lambda e: show_report(
                        person["id"],
                        display_name(person),
                        after_report,
                    ),
                )
            ],
        )

        show_screen(
            make_scrollable([
                header,
                *top_banners,
                heading(
                    "Discover",
                    f"Viewing {idx + 1} of {len(queue)} available members",
                ),
                panel(
                    ft.Column(
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        ),
                        spacing=12,
                        controls=card_controls,
                    ),
                    padding=24,
                ),
                notice,
                actions,
                report_row,
            ]),
            navigation=True,
        )

    # ========================================================
    # USER SIDE: MATCHES
    # ========================================================

    def show_matches():
        try:
            matches = get_matches(current_user_id)
        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load matches",
                    ex,
                    open_dashboard,
                ),
                navigation=True,
            )
            return

        notice = make_notice()

        def handle_unmatch(person):
            try:
                unmatch_users(current_user_id, person["id"])
                state["queue"] = None
                show_matches()
                notify(f"Unmatched with {display_name(person)}.", GREEN)
            except Exception as ex:
                notify(f"Could not unmatch: {ex}")

        controls = [
            app_logo(25),
            heading(
                "Your matches",
                f"{len(matches)} locked mutual connection(s)",
            ),
            notice,
        ]

        if not matches:
            controls.append(
                empty_view(
                    ft.Icons.FAVORITE_BORDER,
                    "No matches yet",
                    "Like profiles in Discover. When someone likes you "
                    "back, your match will lock in here.",
                )
            )
        else:
            for person in matches:
                controls.append(
                    person_row(
                        person,
                        person_subtitle(person),
                        lambda e, pid=person["id"]: open_chat(
                            pid,
                            "Matches",
                        ),
                        trailing=ft.Row(
                            spacing=0,
                            controls=[
                                ft.IconButton(
                                    icon=ft.Icons.CHAT_BUBBLE_OUTLINE,
                                    icon_color=PRIMARY,
                                    tooltip="Message",
                                    on_click=lambda e, pid=person["id"]: open_chat(
                                        pid,
                                        "Matches",
                                    ),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.PERSON_REMOVE_OUTLINED,
                                    icon_color=MUTED,
                                    tooltip="Unmatch",
                                    on_click=lambda e, p=person: handle_unmatch(p),
                                ),
                            ],
                        ),
                    )
                )

        show_screen(
            make_scrollable(controls),
            navigation=True,
        )

    # ========================================================
    # USER SIDE: MESSAGES LIST
    # ========================================================

    def show_messages():
        try:
            matches = get_matches(current_user_id)
            rows = []

            for person in matches:
                last = get_last_message(
                    current_user_id,
                    person["id"],
                )
                rows.append((person, last))

        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load messages",
                    ex,
                    open_dashboard,
                ),
                navigation=True,
            )
            return

        rows.sort(
            key=lambda item: (item[1] or {}).get("created_at") or "",
            reverse=True,
        )

        controls = [
            app_logo(25),
            heading(
                "Messages",
                "Continue conversations with your matches.",
            ),
        ]

        if not rows:
            controls.append(
                empty_view(
                    ft.Icons.CHAT_BUBBLE_OUTLINE,
                    "No conversations yet",
                    "You need a mutual match before you can "
                    "start messaging.",
                )
            )
        else:
            for person, last in rows:
                if last:
                    mine = (
                        str(last.get("sender_id"))
                        == str(current_user_id)
                    )
                    preview = ("You: " if mine else "") + last["message"]
                else:
                    preview = "Say hello \U0001f44b"

                controls.append(
                    person_row(
                        person,
                        preview,
                        lambda e, pid=person["id"]: open_chat(
                            pid,
                            "Messages",
                        ),
                    )
                )

        show_screen(
            make_scrollable(controls),
            navigation=True,
        )

    # ========================================================
    # USER SIDE: PRIVATE CHAT
    # ========================================================

    def open_chat(other_user_id, origin_tab="Messages"):
        nonlocal current_tab

        try:
            person = get_profile(other_user_id)

            if (
                not person
                or is_blocked(person)
                or not is_matched(
                    current_user_id,
                    other_user_id,
                )
            ):
                set_tab("Messages")
                return

        except Exception:
            set_tab("Messages")
            return

        state["chat_token"] += 1
        token = state["chat_token"]
        shown = {"count": -1}

        notice = make_notice()

        message_input = ft.TextField(
            hint_text="Write a message...",
            expand=True,
            filled=True,
            bgcolor=WHITE,
            border_radius=20,
            text_size=14,
        )

        chat_list = ft.ListView(
            expand=True,
            spacing=10,
            auto_scroll=True,
        )

        def bubble(msg):
            mine = (
                str(msg.get("sender_id"))
                == str(current_user_id)
            )

            return ft.Column(
                horizontal_alignment=(
                    ft.CrossAxisAlignment.END
                    if mine
                    else ft.CrossAxisAlignment.START
                ),
                controls=[
                    ft.Container(
                        bgcolor=PRIMARY if mine else LIGHT_PINK,
                        border_radius=14,
                        padding=ft.Padding(
                            left=14,
                            top=9,
                            right=14,
                            bottom=9,
                        ),
                        content=ft.Text(
                            msg.get("message", ""),
                            color=WHITE if mine else DARK,
                            size=14,
                            selectable=True,
                        ),
                    )
                ],
            )

        def render(messages, update=True):
            if messages:
                chat_list.controls = [bubble(m) for m in messages]
            else:
                chat_list.controls = [
                    ft.Container(
                        padding=20,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Text(
                            "You matched! Say hello \U0001f44b",
                            color=MUTED,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                ]

            shown["count"] = len(messages)

            if update:
                page.update()

        def send_message(e=None):
            message = (message_input.value or "").strip()

            if not message:
                return

            if len(message) > 2000:
                notify("Messages cannot exceed 2,000 characters.")
                return

            try:
                if not save_message(
                    current_user_id,
                    other_user_id,
                    message,
                ):
                    notify("Unable to send this message.")
                    return

                message_input.value = ""

                render(
                    get_conversation(
                        current_user_id,
                        other_user_id,
                    )
                )

            except Exception as ex:
                notify(f"Message error: {ex}")

        message_input.on_submit = send_message

        try:
            render(
                get_conversation(current_user_id, other_user_id),
                update=False,
            )
        except Exception as ex:
            chat_list.controls = [
                ft.Text(
                    f"Could not load conversation: {ex}",
                    color=PRIMARY,
                )
            ]

        page.clean()
        page.bgcolor = BG
        page.padding = 0

        page.add(
            ft.Column(
                expand=True,
                spacing=0,
                controls=[
                    ft.Container(
                        padding=10,
                        bgcolor=WHITE,
                        content=ft.Row(
                            controls=[
                                ft.IconButton(
                                    icon=ft.Icons.ARROW_BACK,
                                    icon_color=PRIMARY,
                                    on_click=lambda e: set_tab(
                                        origin_tab
                                    ),
                                ),
                                person_avatar(42, person),
                                ft.Column(
                                    expand=True,
                                    spacing=2,
                                    controls=[
                                        ft.Row(
                                            spacing=4,
                                            controls=[
                                                ft.Text(
                                                    display_name(person),
                                                    weight=ft.FontWeight.BOLD,
                                                    color=DARK,
                                                ),
                                                verified_badge(person),
                                            ],
                                        ),
                                        ft.Text(
                                            "Locked AROVI Match",
                                            size=11,
                                            color=GREEN,
                                        ),
                                    ],
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.FLAG_OUTLINED,
                                    icon_color=MUTED,
                                    tooltip="Report or Block",
                                    on_click=lambda e: show_report(
                                        other_user_id,
                                        display_name(person),
                                        lambda: set_tab(origin_tab),
                                    ),
                                ),
                            ]
                        ),
                    ),
                    ft.Container(
                        expand=True,
                        padding=12,
                        content=chat_list,
                    ),
                    ft.Container(
                        padding=10,
                        bgcolor=WHITE,
                        content=ft.Column(
                            spacing=5,
                            controls=[
                                notice,
                                ft.Row(
                                    controls=[
                                        message_input,
                                        ft.IconButton(
                                            icon=ft.Icons.SEND,
                                            icon_color=PRIMARY,
                                            on_click=send_message,
                                        ),
                                    ]
                                ),
                            ],
                        ),
                    ),
                ],
            )
        )

        page.update()

        async def poll():
            while state["chat_token"] == token:
                await asyncio.sleep(4)

                if state["chat_token"] != token:
                    break

                try:
                    rows = await asyncio.to_thread(
                        get_conversation,
                        current_user_id,
                        other_user_id,
                    )

                    if (
                        state["chat_token"] == token
                        and len(rows) != shown["count"]
                    ):
                        render(rows)

                except Exception:
                    pass

        page.run_task(poll)

    # ========================================================
    # USER SIDE: PROFILE
    # ========================================================

    def show_profile():
        try:
            user = get_profile(current_user_id)
        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load your profile",
                    ex,
                    open_dashboard,
                ),
                navigation=True,
            )
            return

        if not user:
            open_dashboard()
            return

        notice = make_notice()

        name = field("Full name")
        name.value = user.get("name") or ""

        age = field(
            "Age",
            keyboard=ft.KeyboardType.NUMBER,
        )
        age.value = str(user.get("age") or "")

        location = field("Location")
        location.value = user.get("location") or ""

        bio = ft.TextField(
            label="About me",
            value=user.get("bio") or "",
            multiline=True,
            min_lines=3,
            max_lines=5,
            filled=True,
            bgcolor=WHITE,
            border_radius=12,
        )

        gender = ft.Dropdown(
            label="Gender",
            value=user.get("gender") or None,
            options=[
                ft.dropdown.Option("Man"),
                ft.dropdown.Option("Woman"),
                ft.dropdown.Option("Non-binary"),
            ],
        )

        interested = ft.Dropdown(
            label="Interested in",
            value=user.get("interested_in") or None,
            options=[
                ft.dropdown.Option("Men"),
                ft.dropdown.Option("Women"),
                ft.dropdown.Option("Non-binary"),
                ft.dropdown.Option("Everyone"),
            ],
        )

        visible = ft.Switch(
            label="Show my profile in Discover",
            value=user.get("is_active") is not False,
            active_color=PRIMARY,
        )

        def save_profile(e):
            updated_name = (name.value or "").strip()

            try:
                updated_age = int(age.value or "0")
            except ValueError:
                notify("Enter a valid age.")
                return

            if not updated_name:
                notify("Your name cannot be empty.")
                return

            if updated_age < 18 or updated_age > 120:
                notify("AROVI is only for adults aged 18 and above.")
                return

            if not gender.value or not interested.value:
                notify("Select your gender and preference.")
                return

            try:
                api().table("profiles").update({
                    "name": updated_name,
                    "age": updated_age,
                    "location": (location.value or "").strip(),
                    "bio": (bio.value or "").strip()[:500],
                    "gender": gender.value,
                    "interested_in": interested.value,
                    "is_active": bool(visible.value),
                    "updated_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }).eq(
                    "id",
                    str(current_user_id),
                ).execute()

                state["queue"] = None

                notify(
                    "Profile updated successfully.",
                    GREEN,
                )

            except Exception as ex:
                notify(f"Profile update error: {ex}")

        def logout(e):
            nonlocal current_user_id, current_tab

            try:
                api().auth.sign_out()
            except Exception:
                pass

            current_user_id = None
            current_tab = "Discover"
            state.update(
                queue=None,
                index=0,
                user=None,
                admin=False,
                admin_mode=False,
            )

            show_welcome()

        try:
            photos = get_photos(current_user_id)
        except Exception as ex:
            photos = []
            notice.value = f"Could not load photos: {ex}"

        async def add_photos(e):
            remaining = MAX_PHOTOS - len(photos)

            if remaining <= 0:
                notify(
                    f"You can add up to {MAX_PHOTOS} photos. "
                    "Delete one first."
                )
                return

            picker_args = dict(
                dialog_title="Choose photos",
                file_type=ft.FilePickerFileType.IMAGE,
                allow_multiple=True,
            )

            try:
                try:
                    picked = await file_picker.pick_files(
                        with_data=True,
                        **picker_args,
                    )
                except TypeError:
                    picked = await file_picker.pick_files(**picker_args)
            except Exception as ex:
                notify(f"Could not open the photo picker: {ex}")
                return

            if not picked:
                return

            notify("Uploading...", MUTED)

            added = 0
            last_error = None

            for f in picked[:remaining]:
                try:
                    data = getattr(f, "bytes", None)

                    if not data and getattr(f, "path", None):
                        with open(f.path, "rb") as handle:
                            data = handle.read()

                    if not data:
                        raise ValueError(
                            "the phone did not send the image data"
                        )

                    await asyncio.to_thread(
                        upload_photo,
                        current_user_id,
                        f.name,
                        data,
                    )
                    added += 1

                except Exception as ex:
                    last_error = f"{f.name}: {ex}"

            if added:
                show_profile()
                notify(
                    f"{added} photo(s) uploaded."
                    + (f" Problem: {last_error}" if last_error else ""),
                    GREEN if not last_error else PRIMARY,
                )
            else:
                notify(last_error or "No photo was uploaded.")

        def photo_tile(photo):
            is_main = photo["path"] == user.get("avatar_path")

            def make_main(e):
                try:
                    set_main_photo(current_user_id, photo["path"])
                except Exception as ex:
                    notify(f"Could not set main photo: {ex}")
                    return
                show_profile()

            def remove(e):
                try:
                    delete_photo(current_user_id, photo)
                except Exception as ex:
                    notify(f"Could not delete photo: {ex}")
                    return
                show_profile()

            return ft.Column(
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(
                        width=104,
                        height=104,
                        border_radius=12,
                        on_click=lambda e: show_photo_viewer(
                            [p["url"] for p in photos],
                            photos.index(photo),
                        ),
                        bgcolor=LIGHT_PINK,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        content=ft.Image(
                            src=photo["url"],
                            width=104,
                            height=104,
                            fit=FIT_COVER,
                        ),
                    ),
                    ft.Row(
                        spacing=0,
                        alignment=ft.MainAxisAlignment.CENTER,
                        controls=[
                            ft.IconButton(
                                icon=(
                                    ft.Icons.STAR
                                    if is_main
                                    else ft.Icons.STAR_BORDER
                                ),
                                icon_color=GOLD if is_main else MUTED,
                                icon_size=20,
                                tooltip="Main photo",
                                on_click=make_main,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=MUTED,
                                icon_size=20,
                                tooltip="Delete photo",
                                on_click=remove,
                            ),
                        ],
                    ),
                ],
            )

        photo_section = panel(
            ft.Column(
                spacing=10,
                controls=[
                    ft.Text(
                        f"My photos ({len(photos)}/{MAX_PHOTOS})",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    ft.Text(
                        "The starred photo is your main picture. "
                        "Everyone on Discover can see your gallery.",
                        size=12,
                        color=MUTED,
                    ),
                    (
                        ft.Row(
                            wrap=True,
                            spacing=10,
                            run_spacing=6,
                            controls=[photo_tile(p) for p in photos],
                        )
                        if photos
                        else ft.Text(
                            "No photos yet.",
                            size=13,
                            color=MUTED,
                        )
                    ),
                    ft.OutlinedButton(
                        content="ADD PHOTOS",
                        icon=ft.Icons.ADD_A_PHOTO_OUTLINED,
                        on_click=add_photos,
                    ),
                ],
            )
        )

        content = make_scrollable([
            app_logo(25),
            heading(
                "My profile",
                "This is what others see on Discover.",
            ),
            panel(
                ft.Column(
                    horizontal_alignment=(
                        ft.CrossAxisAlignment.CENTER
                    ),
                    spacing=8,
                    controls=[
                        person_avatar(84, user),
                        ft.Row(
                            alignment=ft.MainAxisAlignment.CENTER,
                            spacing=4,
                            controls=[
                                ft.Text(
                                    display_name(user),
                                    weight=ft.FontWeight.BOLD,
                                    color=DARK,
                                ),
                                verified_badge(user),
                            ],
                        ),
                        ft.Text(
                            user.get("email") or "",
                            color=MUTED,
                            size=12,
                        ),
                    ],
                )
            ),
            photo_section,
            name,
            age,
            gender,
            interested,
            location,
            bio,
            visible,
            notice,
            wide_button(
                "SAVE PROFILE",
                save_profile,
            ),
            ft.OutlinedButton(
                content="CHANGE PASSWORD",
                icon=ft.Icons.LOCK_OUTLINE,
                on_click=lambda e: show_change_password(show_profile),
            ),
            ft.OutlinedButton(
                content="LOG OUT",
                icon=ft.Icons.LOGOUT,
                on_click=logout,
            ),
            ft.Text(
                "AROVI \u2022 18+ COMMUNITY",
                size=10,
                color=MUTED,
                text_align=ft.TextAlign.CENTER,
            ),
        ])

        show_screen(
            content,
            navigation=True,
        )

    # ========================================================
    # CHANGE PASSWORD (MEMBERS AND ADMINS)
    # ========================================================

    def show_change_password(back):
        notice = make_notice()

        current = field("Current password", password=True)
        fresh = field("New password", password=True)
        confirm = field("Confirm new password", password=True)

        def save(e):
            old = current.value or ""
            new = fresh.value or ""

            if not old or not new:
                notify("Fill in all the fields.")
                return

            if len(new) < 8:
                notify("New password must be at least 8 characters.")
                return

            if new != (confirm.value or ""):
                notify("The new passwords do not match.")
                return

            if new == old:
                notify("Choose a password different from the current one.")
                return

            try:
                profile = get_profile(current_user_id) or {}
                mail = profile.get("email") or ""

                if not mail:
                    mail = getattr(get_auth_user(), "email", "") or ""

                api().auth.sign_in_with_password({
                    "email": mail,
                    "password": old,
                })
            except Exception:
                notify("Your current password is incorrect.")
                return

            try:
                api().auth.update_user({"password": new})
            except Exception as ex:
                notify(f"Could not change password: {ex}")
                return

            back()
            notify("Password changed.", GREEN)

        is_admin_session = bool(state.get("admin"))

        show_screen(
            make_scrollable([
                app_logo(25),
                heading(
                    "Change password",
                    "Enter your current password, then choose a new one.",
                ),
                current,
                fresh,
                confirm,
                notice,
                wide_button("SAVE NEW PASSWORD", save),
                secondary_button("CANCEL", lambda e: back()),
            ]),
            navigation=not is_admin_session,
            nav=admin_nav() if is_admin_session else None,
        )

    # ========================================================
    # DEDICATED ADMIN CONSOLE (COMPLETELY DISTINCT FROM USER UI)
    # Tabs: Dashboard | Users | Broadcasts | Moderation
    # ========================================================

    ADMIN_PAGE = 20

    def set_atab(tab):
        state["atab"] = tab
        open_admin()

    def admin_nav():
        return build_nav(
            [
                ("Dashboard", ft.Icons.ADMIN_PANEL_SETTINGS_OUTLINED),
                ("Users", ft.Icons.MANAGE_ACCOUNTS_OUTLINED),
                ("Broadcasts", ft.Icons.CAMPAIGN_OUTLINED),
                ("Moderation", ft.Icons.SHIELD_OUTLINED),
            ],
            state.get("atab", "Dashboard"),
            set_atab,
            bar_bg=ADMIN_DARK,
            active_color=GOLD,
            inactive_color="#B9A6B2",
        )

    def admin_screen(content):
        show_screen(content, nav=admin_nav(), bg="#F5EFF3")

    def admin_error(title, ex, retry):
        show_screen(error_view(title, ex, retry), nav=admin_nav(), bg="#F5EFF3")

    def admin_logout(e=None):
        nonlocal current_user_id, current_tab

        try:
            api().auth.sign_out()
        except Exception:
            pass

        current_user_id = None
        current_tab = "Discover"
        state.update(
            queue=None,
            index=0,
            user=None,
            admin=False,
            admin_mode=False,
            atab="Dashboard",
            asearch="",
            afilter="all",
            apage=0,
            aphoto_page=0,
            areport="open",
        )

        show_welcome()

    def admin_header(title, subtitle=None):
        return ft.Container(
            bgcolor=ADMIN_DARK,
            border_radius=16,
            padding=16,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Column(
                        expand=True,
                        spacing=2,
                        controls=[
                            ft.Text(
                                "AROVI ADMIN CONSOLE",
                                size=10,
                                weight=ft.FontWeight.BOLD,
                                color=GOLD,
                            ),
                            ft.Text(
                                title,
                                size=21,
                                weight=ft.FontWeight.BOLD,
                                color=WHITE,
                            ),
                            ft.Text(
                                subtitle or "",
                                size=12,
                                color="#D4C4CE",
                                visible=bool(subtitle),
                            ),
                        ],
                    ),
                    ft.Row(
                        spacing=0,
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.LOCK_RESET,
                                icon_color=GOLD,
                                tooltip="Change Admin Password",
                                on_click=lambda e: show_change_password(
                                    open_admin
                                ),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.LOGOUT,
                                icon_color=WHITE,
                                tooltip="Exit Admin Console",
                                on_click=admin_logout,
                            ),
                        ],
                    ),
                ],
            ),
        )

    def admin_run(action, ok_message, after):
        try:
            action()
        except Exception as ex:
            after()
            notify(f"Failed: {ex}")
            return

        after()
        notify(ok_message, GREEN)

    def admin_confirm(title, message, yes_label, on_yes, back):
        show_screen(
            ft.Column(
                expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=16,
                controls=[
                    centered_icon(ft.Icons.WARNING_AMBER_ROUNDED),
                    ft.Text(
                        title,
                        size=20,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        message,
                        size=13,
                        color=MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    wide_button(yes_label, lambda e: on_yes(), bgcolor=RED),
                    secondary_button("CANCEL", lambda e: back()),
                ],
            ),
            nav=admin_nav(),
            bg="#F5EFF3",
        )

    def outline_wide(label, action, icon=None):
        return ft.Row(
            controls=[
                ft.OutlinedButton(
                    content=label,
                    icon=icon,
                    on_click=action,
                    expand=True,
                )
            ]
        )

    def chip(label, selected, on_tap):
        return ft.Container(
            bgcolor=ADMIN_DARK if selected else WHITE,
            border_radius=20,
            padding=ft.Padding(left=14, top=7, right=14, bottom=7),
            on_click=on_tap,
            content=ft.Text(
                label,
                size=12,
                weight=ft.FontWeight.BOLD,
                color=GOLD if selected else DARK,
            ),
        )

    def pager(page_number, has_next, go):
        return ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            controls=[
                ft.TextButton(
                    content="< Previous",
                    visible=page_number > 0,
                    on_click=lambda e: go(page_number - 1),
                ),
                ft.Text(
                    f"Page {page_number + 1}",
                    size=12,
                    color=MUTED,
                ),
                ft.TextButton(
                    content="Next >",
                    visible=has_next,
                    on_click=lambda e: go(page_number + 1),
                ),
            ],
        )

    def admin_subtitle(person):
        tags = [(norm(person.get("status")) or "active").upper()]

        if person.get("is_verified"):
            tags.append("VERIFIED")

        if person.get("is_admin"):
            tags.append("ADMIN")

        if person.get("is_active") is False:
            tags.append("HIDDEN")

        parts = [" | ".join(tags)]
        if person.get("email"):
            parts.append(person["email"])

        return " \u2022 ".join(parts)

    def open_admin():
        nonlocal current_user_id

        state["chat_token"] += 1

        if not current_user_id:
            show_welcome()
            return

        try:
            allowed = check_admin(current_user_id)
        except Exception as ex:
            show_screen(
                error_view("Connection problem", ex, open_admin)
            )
            return

        if not allowed:
            try:
                api().auth.sign_out()
            except Exception:
                pass

            current_user_id = None
            state.update(admin=False, admin_mode=False)

            show_welcome()
            return

        state["admin"] = True
        tab = state.get("atab", "Dashboard")

        if tab == "Users":
            admin_users()
        elif tab == "Broadcasts":
            admin_broadcasts()
        elif tab == "Moderation":
            admin_moderation()
        else:
            admin_dashboard()

    # ---------- ADMIN TAB 1: DASHBOARD ----------

    def admin_dashboard():
        try:
            stats = admin_stats()
            recent = admin_list_users("", "all", 0, 5)
            live_ann = get_active_announcement()
        except Exception as ex:
            admin_error("Could not load admin dashboard", ex, admin_dashboard)
            return

        items = [
            ("Total Members", "members", ADMIN_DARK),
            ("New Today", "new_today", GREEN),
            ("New This Week", "new_week", PRIMARY),
            ("Active", "active", GREEN),
            ("Suspended", "suspended", WARNING_TEXT),
            ("Banned", "banned", RED),
            ("Hidden Profiles", "hidden", MUTED),
            ("Admins", "admins", GOLD),
            ("Uploaded Photos", "photos", PRIMARY),
            ("Mutual Matches", "matches", PRIMARY),
            ("Open Reports", "open_reports", RED),
        ]

        tiles = [
            ft.Container(
                width=155,
                bgcolor=WHITE,
                border_radius=16,
                padding=14,
                content=ft.Column(
                    spacing=2,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            str(stats.get(key, 0)),
                            size=26,
                            weight=ft.FontWeight.BOLD,
                            color=accent,
                        ),
                        ft.Text(label, size=12, color=MUTED),
                    ],
                ),
            )
            for label, key, accent in items
        ]

        quick_actions = panel(
            ft.Column(
                spacing=10,
                controls=[
                    ft.Text(
                        "Quick Operations",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    ft.Row(
                        wrap=True,
                        spacing=8,
                        run_spacing=8,
                        controls=[
                            ft.Button(
                                content="Manage All Users",
                                icon=ft.Icons.PEOPLE,
                                bgcolor=ADMIN_DARK,
                                color=WHITE,
                                on_click=lambda e: set_atab("Users"),
                            ),
                            ft.Button(
                                content="Send Announcement",
                                icon=ft.Icons.CAMPAIGN,
                                bgcolor=PRIMARY,
                                color=WHITE,
                                on_click=lambda e: set_atab("Broadcasts"),
                            ),
                            ft.Button(
                                content=f"Review Reports ({stats.get('open_reports', 0)})",
                                icon=ft.Icons.FLAG,
                                bgcolor=RED if stats.get("open_reports", 0) else MUTED,
                                color=WHITE,
                                on_click=lambda e: set_atab("Moderation"),
                            ),
                        ],
                    ),
                ],
            )
        )

        ann_status = panel(
            ft.Column(
                spacing=4,
                controls=[
                    ft.Text(
                        "Active Live Banner",
                        size=14,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    ft.Text(
                        f"{live_ann['title']}: {live_ann['body']}"
                        if live_ann
                        else "No active broadcast banner is currently displayed to users.",
                        size=12,
                        color=GREEN if live_ann else MUTED,
                    ),
                ],
            )
        )

        controls = [
            admin_header("System Overview", "Real-time platform metrics & operational controls."),
            make_notice(),
            quick_actions,
            ann_status,
            ft.Row(
                wrap=True,
                spacing=10,
                run_spacing=10,
                controls=tiles,
            ),
            ft.Text(
                "Recently Registered Accounts",
                size=16,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            ),
        ]

        for person in recent:
            controls.append(
                person_row(
                    person,
                    admin_subtitle(person),
                    lambda e, uid=person["id"]: admin_user_detail(
                        uid,
                        admin_dashboard,
                    ),
                )
            )

        controls.append(wide_button("REFRESH TELEMETRY", lambda e: admin_dashboard(), bgcolor=ADMIN_DARK))

        admin_screen(make_scrollable(controls))

    # ---------- ADMIN TAB 2: ALL USERS & FULL PROFILE CONTROL ----------

    def admin_users(search=None, filter_by=None, page=None):
        if search is not None:
            state["asearch"] = search
            state["apage"] = 0

        if filter_by is not None:
            state["afilter"] = filter_by
            state["apage"] = 0

        if page is not None:
            state["apage"] = max(0, page)

        query = state.get("asearch", "")
        current_filter = state.get("afilter", "all")
        page_number = state.get("apage", 0)

        try:
            rows = admin_list_users(
                query,
                current_filter,
                page_number * ADMIN_PAGE,
                ADMIN_PAGE + 1,
            )
            stats = admin_stats()
        except Exception as ex:
            admin_error("Could not load users", ex, admin_users)
            return

        has_next = len(rows) > ADMIN_PAGE
        rows = rows[:ADMIN_PAGE]

        def export_users_csv(e):
            try:
                all_users = admin_list_users("", "all", 0, 5000)
                export_path = BASE_DIR / f"arovi_users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                with open(export_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "ID", "Name", "Email", "Age", "Gender",
                        "Interested In", "Location", "Status",
                        "Verified", "Visible", "Admin", "Created At"
                    ])
                    for u in all_users:
                        writer.writerow([
                            u.get("id"), u.get("name"), u.get("email"),
                            u.get("age"), u.get("gender"), u.get("interested_in"),
                            u.get("location"), u.get("status"),
                            u.get("is_verified"), u.get("is_active"),
                            u.get("is_admin"), u.get("created_at")
                        ])
                notify(f"Exported {len(all_users)} users to {export_path.name}", GREEN)
            except Exception as ex:
                notify(f"CSV export failed: {ex}")

        search_field = ft.TextField(
            hint_text="Search by name, email, or city (press Enter)",
            value=query,
            filled=True,
            bgcolor=WHITE,
            border_radius=12,
            text_size=14,
            on_submit=lambda e: admin_users(search=e.control.value or ""),
        )

        filters = [
            ("All", "all"),
            ("Active", "active"),
            ("Suspended", "suspended"),
            ("Banned", "banned"),
            ("Hidden", "hidden"),
            ("Admins", "admins"),
        ]

        controls = [
            admin_header(
                "User Directory",
                f"{stats.get('members', 0)} registered accounts",
            ),
            search_field,
            ft.Row(
                wrap=True,
                spacing=8,
                run_spacing=8,
                controls=[
                    chip(
                        label,
                        current_filter == value,
                        lambda e, v=value: admin_users(filter_by=v),
                    )
                    for label, value in filters
                ],
            ),
            outline_wide(
                "EXPORT FULL DIRECTORY (CSV)",
                export_users_csv,
                icon=ft.Icons.DOWNLOAD,
            ),
            make_notice(),
        ]

        if not rows:
            controls.append(
                empty_view(
                    ft.Icons.PEOPLE_OUTLINE,
                    "No users found",
                    "Try a different search query or filter.",
                )
            )

        for person in rows:
            controls.append(
                person_row(
                    person,
                    admin_subtitle(person),
                    lambda e, uid=person["id"]: admin_user_detail(uid),
                )
            )

        if page_number > 0 or has_next:
            controls.append(
                pager(
                    page_number,
                    has_next,
                    lambda n: admin_users(page=n),
                )
            )

        admin_screen(make_scrollable(controls))

    def admin_edit_full_profile_screen(person, back):
        notice = make_notice()
        uid = person["id"]

        name_f = field("Full Name")
        name_f.value = person.get("name") or ""

        age_f = field("Age", keyboard=ft.KeyboardType.NUMBER)
        age_f.value = str(person.get("age") or "")

        gender_f = ft.Dropdown(
            label="Gender",
            value=person.get("gender") or "Man",
            options=[
                ft.dropdown.Option("Man"),
                ft.dropdown.Option("Woman"),
                ft.dropdown.Option("Non-binary"),
            ],
        )

        interested_f = ft.Dropdown(
            label="Interested In",
            value=person.get("interested_in") or "Everyone",
            options=[
                ft.dropdown.Option("Men"),
                ft.dropdown.Option("Women"),
                ft.dropdown.Option("Non-binary"),
                ft.dropdown.Option("Everyone"),
            ],
        )

        loc_f = field("City / Location")
        loc_f.value = person.get("location") or ""

        bio_f = field("Profile Bio", multiline=True)
        bio_f.value = person.get("bio") or ""

        status_f = ft.Dropdown(
            label="Account Status",
            value=norm(person.get("status")) or "active",
            options=[
                ft.dropdown.Option("active", "Active (Normal Access)"),
                ft.dropdown.Option("suspended", "Suspended (Temporary Lockout)"),
                ft.dropdown.Option("banned", "Banned (Permanent Block)"),
            ],
        )

        visible_f = ft.Switch(
            label="Visible in public Discover feed",
            value=person.get("is_active") is not False,
            active_color=PRIMARY,
        )

        def save_edits(e):
            try:
                age_val = int(age_f.value or 18)
                if age_val < 18 or age_val > 120:
                    notify("Age must be between 18 and 120.")
                    return
            except ValueError:
                notify("Enter a valid age.")
                return

            if not (name_f.value or "").strip():
                notify("Name cannot be empty.")
                return

            admin_run(
                lambda: admin_update_full_profile(
                    uid,
                    name_f.value.strip(),
                    age_val,
                    gender_f.value or "Man",
                    interested_f.value or "Everyone",
                    (loc_f.value or "").strip(),
                    (bio_f.value or "").strip()[:500],
                    status_f.value or "active",
                    bool(visible_f.value),
                ),
                "All profile attributes updated.",
                back,
            )

        admin_screen(
            make_scrollable([
                admin_header(f"Edit Profile: {display_name(person)}", "Full administrative override."),
                name_f,
                age_f,
                gender_f,
                interested_f,
                loc_f,
                bio_f,
                status_f,
                visible_f,
                notice,
                wide_button("SAVE ALL PROFILE CHANGES", save_edits, bgcolor=ADMIN_DARK),
                secondary_button("CANCEL", lambda e: back()),
            ])
        )

    def admin_send_warning_screen(person, back):
        notice = make_notice()
        uid = person["id"]

        warn_f = field("Official Safety Warning Banner (leave empty to clear)", multiline=True)
        warn_f.value = person.get("admin_warning") or ""

        def send_warn(e):
            admin_run(
                lambda: admin_send_warning(uid, warn_f.value or ""),
                "Member warning banner updated.",
                back,
            )

        admin_screen(
            make_scrollable([
                admin_header(
                    f"Warn {display_name(person)}",
                    "Displays a prominent notice banner on their screen until dismissed.",
                ),
                warn_f,
                notice,
                wide_button("SEND / UPDATE WARNING", send_warn, bgcolor=PRIMARY),
                secondary_button("CANCEL", lambda e: back()),
            ])
        )

    def admin_user_detail(user_id, back=None):
        back = back or admin_users

        try:
            person = get_profile(user_id)

            if not person:
                back()
                return

            summary = admin_user_summary(user_id)
            matches = admin_user_matches(user_id)

            try:
                photos = get_photos(user_id)
            except Exception:
                photos = []

        except Exception as ex:
            admin_error("Could not load this user", ex, back)
            return

        notice = make_notice()

        status = norm(person.get("status")) or "active"
        target_is_admin = bool(person.get("is_admin"))
        target_is_verified = bool(person.get("is_verified"))
        is_self = str(user_id) == str(current_user_id)
        visible_now = person.get("is_active") is not False
        name = display_name(person)

        def refresh():
            admin_user_detail(user_id, back)

        def info(label, value):
            return ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(label, size=13, color=MUTED),
                    ft.Text(
                        str(value if value not in (None, "") else "-"),
                        size=13,
                        color=DARK,
                        text_align=ft.TextAlign.RIGHT,
                        expand=True,
                    ),
                ],
            )

        details = panel(
            ft.Column(
                spacing=8,
                controls=[
                    ft.Text(
                        "Account & Profile Data",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    info("User ID", str(person.get("id"))),
                    info("Email", person.get("email")),
                    info("Full Name", person.get("name")),
                    info("Age", person.get("age")),
                    info("Gender", person.get("gender")),
                    info("Interested In", person.get("interested_in")),
                    info("Location", person.get("location")),
                    info("Account Status", status.upper()),
                    info("Verified Badge", "Verified" if target_is_verified else "Unverified"),
                    info("Admin Privileges", "Yes" if target_is_admin else "No"),
                    info("Discover Visibility", "Visible" if visible_now else "Hidden"),
                    info("Active Warning", person.get("admin_warning") or "None"),
                    info("Registered Date", str(person.get("created_at") or "")[:19].replace("T", " ")),
                    info("Last Updated", str(person.get("updated_at") or "")[:19].replace("T", " ")),
                    ft.Divider(height=1),
                    ft.Text("Bio:", size=12, weight=ft.FontWeight.BOLD, color=MUTED),
                    ft.Text(
                        (person.get("bio") or "No bio provided.").strip(),
                        size=13,
                        color=DARK,
                    ),
                ],
            )
        )

        activity = panel(
            ft.Column(
                spacing=8,
                controls=[
                    ft.Text(
                        "Engagement & Safety Telemetry",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    info("Likes Sent", summary.get("likes_sent", 0)),
                    info("Likes Received", summary.get("likes_received", 0)),
                    info("Active Matches", summary.get("matches", 0)),
                    info("Messages Sent", summary.get("messages_sent", 0)),
                    info("Reports Against User", summary.get("reports_against", 0)),
                    info("Reports Filed by User", summary.get("reports_filed", 0)),
                ],
            )
        )

        def remove_photo(photo):
            admin_run(
                lambda: admin_remove_photo(photo),
                "Photo removed.",
                refresh,
            )

        def photo_tile(index, photo):
            return ft.Column(
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(
                        width=104,
                        height=104,
                        border_radius=12,
                        bgcolor=LIGHT_PINK,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        on_click=lambda e: show_photo_viewer(
                            [p["url"] for p in photos],
                            index,
                        ),
                        content=ft.Image(
                            src=photo["url"],
                            width=104,
                            height=104,
                            fit=FIT_COVER,
                        ),
                    ),
                    ft.TextButton(
                        content="Delete Photo",
                        on_click=lambda e, p=photo: admin_confirm(
                            "Delete this photo?",
                            "The photo will be permanently deleted from storage.",
                            "DELETE PHOTO",
                            lambda: remove_photo(p),
                            refresh,
                        ),
                    ),
                ],
            )

        photo_block = panel(
            ft.Column(
                spacing=10,
                controls=[
                    ft.Text(
                        f"Uploaded Photos ({len(photos)})",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    (
                        ft.Row(
                            wrap=True,
                            spacing=10,
                            run_spacing=6,
                            controls=[
                                photo_tile(i, p)
                                for i, p in enumerate(photos)
                            ],
                        )
                        if photos
                        else ft.Text("No photos uploaded.", size=13, color=MUTED)
                    ),
                ],
            )
        )

        match_controls = [
            ft.Text(
                f"Active Matches ({len(matches)})",
                size=16,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            )
        ]

        if not matches:
            match_controls.append(
                ft.Text("No active matches.", size=13, color=MUTED)
            )

        for m in matches:
            match_controls.append(
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    controls=[
                        ft.TextButton(
                            content=m.get("other_name") or "Member",
                            on_click=lambda e, uid=m["other_id"]: admin_user_detail(
                                uid,
                                refresh,
                            ),
                        ),
                        ft.TextButton(
                            content="Force Unmatch",
                            on_click=lambda e, mid=m["match_id"], n=m.get("other_name"): admin_confirm(
                                "Force unmatch these members?",
                                f"{name} and {n or 'this member'} will be unmatched immediately.",
                                "FORCE UNMATCH",
                                lambda: admin_run(
                                    lambda: admin_unmatch(mid),
                                    "Unmatched.",
                                    refresh,
                                ),
                                refresh,
                            ),
                        ),
                    ],
                )
            )

        match_block = panel(
            ft.Column(spacing=4, controls=match_controls)
        )

        actions = [
            wide_button(
                "EDIT ALL PROFILE FIELDS",
                lambda e: admin_edit_full_profile_screen(person, refresh),
                bgcolor=ADMIN_DARK,
            ),
            outline_wide(
                "REMOVE VERIFIED BADGE" if target_is_verified else "GRANT VERIFIED BADGE",
                lambda e: admin_run(
                    lambda: admin_set_verified(user_id, not target_is_verified),
                    "Verification badge updated.",
                    refresh,
                ),
                icon=ft.Icons.VERIFIED,
            ),
        ]

        if is_self:
            actions.append(
                ft.Text(
                    "Note: You are viewing your own administrator account.",
                    size=12,
                    color=MUTED,
                )
            )
        elif target_is_admin:
            actions.append(
                outline_wide(
                    "REVOKE ADMIN PRIVILEGES",
                    lambda e: admin_confirm(
                        f"Remove admin role from {name}?",
                        "They will be downgraded to a standard member account.",
                        "REVOKE ADMIN",
                        lambda: admin_run(
                            lambda: admin_set_admin(user_id, False),
                            "Admin privileges revoked.",
                            refresh,
                        ),
                        refresh,
                    ),
                )
            )
        else:
            actions.append(
                outline_wide(
                    "SEND / EDIT OFFICIAL WARNING",
                    lambda e: admin_send_warning_screen(person, refresh),
                    icon=ft.Icons.WARNING_AMBER,
                )
            )

            if status == "active":
                actions.append(
                    wide_button(
                        "SUSPEND ACCOUNT",
                        lambda e: admin_run(
                            lambda: admin_set_status(user_id, "suspended"),
                            "Account suspended.",
                            refresh,
                        ),
                        bgcolor=WARNING_TEXT,
                    )
                )
                actions.append(
                    wide_button(
                        "BAN ACCOUNT",
                        lambda e: admin_confirm(
                            f"Ban {name}?",
                            "They will be locked out and hidden from all members.",
                            "BAN ACCOUNT",
                            lambda: admin_run(
                                lambda: admin_set_status(user_id, "banned"),
                                "Account banned.",
                                refresh,
                            ),
                            refresh,
                        ),
                        bgcolor=RED,
                    )
                )
            else:
                actions.append(
                    wide_button(
                        "REACTIVATE ACCOUNT",
                        lambda e: admin_run(
                            lambda: admin_set_status(user_id, "active"),
                            "Account reactivated.",
                            refresh,
                        ),
                        bgcolor=GREEN,
                    )
                )

            actions.append(
                outline_wide(
                    "HIDE FROM DISCOVER" if visible_now else "SHOW IN DISCOVER",
                    lambda e: admin_run(
                        lambda: admin_set_visibility(user_id, not visible_now),
                        "Discover visibility updated.",
                        refresh,
                    ),
                )
            )

            if (person.get("bio") or "").strip():
                actions.append(
                    outline_wide(
                        "WIPE BIO TEXT",
                        lambda e: admin_confirm(
                            "Clear this bio?",
                            "The member's bio text will be removed.",
                            "CLEAR BIO",
                            lambda: admin_run(
                                lambda: admin_clear_bio(user_id),
                                "Bio cleared.",
                                refresh,
                            ),
                            refresh,
                        ),
                    )
                )

            actions.append(
                outline_wide(
                    "PROMOTE TO ADMIN",
                    lambda e: admin_confirm(
                        f"Promote {name} to Admin?",
                        "They will gain full access to the Admin Console.",
                        "PROMOTE TO ADMIN",
                        lambda: admin_run(
                            lambda: admin_set_admin(user_id, True),
                            "Admin role granted.",
                            refresh,
                        ),
                        refresh,
                    ),
                )
            )

            actions.append(
                wide_button(
                    "PERMANENTLY DELETE USER",
                    lambda e: admin_confirm(
                        f"Permanently delete {name}?",
                        "This erases the user account, photos, likes, matches, "
                        "and messages forever.",
                        "DELETE FOREVER",
                        lambda: admin_run(
                            lambda: admin_delete_user(user_id),
                            "User permanently deleted.",
                            admin_users,
                        ),
                        refresh,
                    ),
                    bgcolor=RED,
                )
            )

        admin_screen(
            make_scrollable([
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.ARROW_BACK,
                            icon_color=ADMIN_DARK,
                            on_click=lambda e: back(),
                        ),
                        person_avatar(54, person),
                        ft.Column(
                            expand=True,
                            spacing=2,
                            controls=[
                                ft.Row(
                                    spacing=4,
                                    controls=[
                                        ft.Text(
                                            name,
                                            size=19,
                                            weight=ft.FontWeight.BOLD,
                                            color=DARK,
                                        ),
                                        verified_badge(person),
                                    ],
                                ),
                                ft.Text(
                                    admin_subtitle(person),
                                    size=12,
                                    color=MUTED,
                                ),
                            ],
                        ),
                    ]
                ),
                notice,
                details,
                activity,
                photo_block,
                match_block,
                panel(
                    ft.Column(
                        spacing=10,
                        controls=[
                            ft.Text(
                                "Administrative Actions",
                                size=16,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            *actions,
                        ],
                    )
                ),
            ])
        )

    # ---------- ADMIN TAB 3: ANNOUNCEMENTS & BROADCASTS ----------

    def admin_broadcasts():
        try:
            history = admin_list_announcements()
        except Exception as ex:
            admin_error("Could not load broadcasts", ex, admin_broadcasts)
            return

        notice = make_notice()
        title_f = field("Announcement Headline (e.g., Weekend Verification Drive)")
        body_f = field("Message Body (displayed to all users on Discover)", multiline=True)

        def publish(e):
            if not (title_f.value or "").strip() or not (body_f.value or "").strip():
                notify("Enter both a headline and message body.")
                return
            admin_run(
                lambda: admin_publish_announcement(title_f.value, body_f.value),
                "Announcement published live to all users.",
                admin_broadcasts,
            )

        controls = [
            admin_header("Announcements", "Broadcast system updates & community notices."),
            make_notice(),
            panel(
                ft.Column(
                    spacing=10,
                    controls=[
                        ft.Text(
                            "Create New Broadcast",
                            size=16,
                            weight=ft.FontWeight.BOLD,
                            color=DARK,
                        ),
                        ft.Text(
                            "Publishing a new announcement automatically sets it as the live banner across the app.",
                            size=12,
                            color=MUTED,
                        ),
                        title_f,
                        body_f,
                        wide_button("PUBLISH LIVE ANNOUNCEMENT", publish, bgcolor=ADMIN_DARK),
                    ],
                )
            ),
            ft.Text(
                "Broadcast History",
                size=16,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            ),
        ]

        if not history:
            controls.append(
                empty_view(
                    ft.Icons.CAMPAIGN_OUTLINED,
                    "No announcements yet",
                    "Published announcements will appear here for management.",
                )
            )

        for ann in history:
            is_live = bool(ann.get("is_active"))
            controls.append(
                panel(
                    ft.Column(
                        spacing=6,
                        controls=[
                            ft.Row(
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                controls=[
                                    ft.Text(
                                        ann.get("title") or "Untitled",
                                        size=15,
                                        weight=ft.FontWeight.BOLD,
                                        color=DARK,
                                        expand=True,
                                    ),
                                    ft.Container(
                                        bgcolor=GREEN if is_live else MUTED,
                                        border_radius=8,
                                        padding=ft.Padding(left=8, top=3, right=8, bottom=3),
                                        content=ft.Text(
                                            "LIVE" if is_live else "INACTIVE",
                                            size=10,
                                            color=WHITE,
                                            weight=ft.FontWeight.BOLD,
                                        ),
                                    ),
                                ],
                            ),
                            ft.Text(ann.get("body") or "", size=13, color=DARK),
                            ft.Text(
                                f"Created: {str(ann.get('created_at') or '')[:16].replace('T', ' ')}",
                                size=11,
                                color=MUTED,
                            ),
                            ft.Row(
                                controls=[
                                    ft.TextButton(
                                        content="Deactivate" if is_live else "Set Live",
                                        on_click=lambda e, aid=ann["id"], act=not is_live: admin_run(
                                            lambda: admin_toggle_announcement(aid, act),
                                            "Broadcast status updated.",
                                            admin_broadcasts,
                                        ),
                                    ),
                                    ft.TextButton(
                                        content="Delete",
                                        on_click=lambda e, aid=ann["id"]: admin_run(
                                            lambda: admin_delete_announcement(aid),
                                            "Announcement deleted.",
                                            admin_broadcasts,
                                        ),
                                    ),
                                ],
                            ),
                        ],
                    )
                )
            )

        admin_screen(make_scrollable(controls))

    # ---------- ADMIN TAB 4: MODERATION (REPORTS + PHOTO AUDIT) ----------

    def admin_view_chat(first_id, second_id, names):
        try:
            messages = admin_get_conversation(first_id, second_id)
        except Exception as ex:
            admin_error("Could not load conversation", ex, admin_moderation)
            return

        controls = [
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        icon_color=ADMIN_DARK,
                        on_click=lambda e: admin_moderation(),
                    ),
                    ft.Text(
                        "Reported Chat Transcript",
                        size=18,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                ]
            ),
            ft.Text(
                "Accessed strictly for safety & abuse investigation.",
                size=12,
                color=MUTED,
            ),
        ]

        if not messages:
            controls.append(
                ft.Text("No messages recorded between these members.", color=MUTED)
            )

        for msg in messages:
            sender = names.get(str(msg.get("sender_id")), "Member")
            stamp = str(msg.get("created_at") or "")[:16].replace("T", " ")

            controls.append(
                ft.Column(
                    spacing=2,
                    controls=[
                        ft.Text(
                            f"{sender} \u2022 {stamp}",
                            size=11,
                            color=MUTED,
                        ),
                        ft.Container(
                            bgcolor=WHITE,
                            border_radius=12,
                            padding=10,
                            content=ft.Text(
                                msg.get("message", ""),
                                size=13,
                                color=DARK,
                                selectable=True,
                            ),
                        ),
                    ],
                )
            )

        admin_screen(make_scrollable(controls))

    def admin_moderation(which=None, photo_page=None):
        if which is not None:
            state["areport"] = which

        if photo_page is not None:
            state["aphoto_page"] = max(0, photo_page)

        mode = state.get("areport", "open")
        p_page = state.get("aphoto_page", 0)
        p_size = 6

        try:
            reports = admin_list_reports(mode)
            photos = admin_recent_photos(p_page * p_size, p_size + 1)
            has_next_photos = len(photos) > p_size
            photos = photos[:p_size]
            urls = signed_urls([r["path"] for r in photos]) if photos else {}
        except Exception as ex:
            admin_error("Could not load moderation queue", ex, admin_moderation)
            return

        names = {}
        for r in reports:
            names[str(r["reporter_id"])] = r.get("reporter_name") or "Member"
            names[str(r["reported_id"])] = r.get("reported_name") or "Member"

        def dismiss(report):
            admin_run(
                lambda: admin_resolve_report(report["id"], "dismissed"),
                "Report dismissed.",
                admin_moderation,
            )

        def sanction(report, status):
            def action():
                admin_set_status(report["reported_id"], status)
                admin_resolve_report(report["id"], "resolved")

            admin_run(
                action,
                f"User {status} and report resolved.",
                admin_moderation,
            )

        controls = [
            admin_header(
                "Safety & Moderation",
                f"{len(reports)} {'open' if mode == 'open' else 'closed'} report(s)",
            ),
            make_notice(),
            ft.Text(
                "Member Abuse Reports",
                size=16,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            ),
            ft.Row(
                spacing=8,
                controls=[
                    chip("Open Reports", mode == "open", lambda e: admin_moderation(which="open")),
                    chip("Closed Reports", mode != "open", lambda e: admin_moderation(which="closed")),
                ],
            ),
        ]

        if not reports:
            controls.append(
                panel(
                    ft.Text(
                        "No reports in this queue.",
                        size=13,
                        color=MUTED,
                    )
                )
            )

        for report in reports:
            target = str(report["reported_id"])
            reporter = str(report["reporter_id"])
            is_open = report.get("status") == "open"

            buttons = [
                ft.TextButton(
                    content="Inspect Profile",
                    on_click=lambda e, t=target: admin_user_detail(
                        t,
                        admin_moderation,
                    ),
                ),
                ft.TextButton(
                    content="Inspect Chat",
                    on_click=lambda e, a=reporter, b=target: admin_view_chat(
                        a,
                        b,
                        names,
                    ),
                ),
            ]

            if is_open:
                buttons += [
                    ft.TextButton(
                        content="Dismiss",
                        on_click=lambda e, r=report: dismiss(r),
                    ),
                    ft.TextButton(
                        content="Suspend",
                        on_click=lambda e, r=report: sanction(r, "suspended"),
                    ),
                    ft.TextButton(
                        content="Ban",
                        on_click=lambda e, r=report: admin_confirm(
                            "Ban this member?",
                            "They will be banned and this report marked resolved.",
                            "BAN USER",
                            lambda: sanction(r, "banned"),
                            admin_moderation,
                        ),
                    ),
                ]

            controls.append(
                panel(
                    ft.Column(
                        spacing=6,
                        controls=[
                            ft.Text(
                                f"Reported: {names.get(target, 'Member')}",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            ft.Text(
                                f"Filed by {names.get(reporter, 'Member')} \u2022 "
                                f"{str(report.get('created_at') or '')[:10]}"
                                + (
                                    ""
                                    if is_open
                                    else f" \u2022 {str(report.get('status')).title()}"
                                ),
                                size=12,
                                color=MUTED,
                            ),
                            ft.Text(
                                report.get("reason") or "",
                                size=13,
                                color=DARK,
                            ),
                            ft.Row(
                                wrap=True,
                                spacing=0,
                                controls=buttons,
                            ),
                        ],
                    )
                )
            )

        controls.append(
            ft.Text(
                "Recent Photo Uploads Audit",
                size=16,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            )
        )

        photo_tiles = []
        for photo in photos:
            url = urls.get(photo["path"])
            if not url:
                continue
            photo_tiles.append(
                ft.Column(
                    spacing=0,
                    width=145,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            width=145,
                            height=145,
                            border_radius=12,
                            bgcolor=LIGHT_PINK,
                            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                            on_click=lambda e, u=url: show_photo_viewer([u], 0),
                            content=ft.Image(
                                src=url,
                                width=145,
                                height=145,
                                fit=FIT_COVER,
                            ),
                        ),
                        ft.Text(
                            photo.get("owner_name") or "Member",
                            size=12,
                            color=DARK,
                            max_lines=1,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Row(
                            spacing=0,
                            alignment=ft.MainAxisAlignment.CENTER,
                            controls=[
                                ft.TextButton(
                                    content="User",
                                    on_click=lambda e, uid=photo["user_id"]: admin_user_detail(
                                        uid,
                                        admin_moderation,
                                    ),
                                ),
                                ft.TextButton(
                                    content="Delete",
                                    on_click=lambda e, p=photo: admin_confirm(
                                        "Delete this photo?",
                                        "The photo is permanently deleted.",
                                        "DELETE PHOTO",
                                        lambda: admin_run(
                                            lambda: admin_remove_photo(p),
                                            "Photo deleted.",
                                            admin_moderation,
                                        ),
                                        admin_moderation,
                                    ),
                                ),
                            ],
                        ),
                    ],
                )
            )

        if photo_tiles:
            controls.append(
                ft.Row(
                    wrap=True,
                    spacing=10,
                    run_spacing=10,
                    alignment=ft.MainAxisAlignment.CENTER,
                    controls=photo_tiles,
                )
            )
        else:
            controls.append(
                panel(ft.Text("No uploaded photos to audit.", size=13, color=MUTED))
            )

        if p_page > 0 or has_next_photos:
            controls.append(
                pager(
                    p_page,
                    has_next_photos,
                    lambda n: admin_moderation(photo_page=n),
                )
            )

        admin_screen(make_scrollable(controls))

    # ========================================================
    # START APPLICATION
    # ========================================================

    if supabase is None:
        show_screen(
            ft.Column(
                expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    app_logo(36),
                    heading("Connect AROVI to Supabase"),
                    ft.Text(
                        supabase_error or "Supabase is not configured.",
                        color=PRIMARY,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "Create a .env file beside main.py and add "
                        "SUPABASE_URL and SUPABASE_KEY. "
                        "Then restart AROVI.",
                        color=MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
            )
        )
    else:
        show_splash()


# ============================================================
# RUN AROVI
# ============================================================

if __name__ == "__main__":
    ft.run(main)
