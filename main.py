import os
import re
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
# Complete single-file application (with built-in admin side)
# ============================================================

APP_NAME = "AROVI"

PRIMARY = "#9B4D69"
DARK = "#321C2B"
GOLD = "#D6A56C"
BG = "#FFF8F5"
WHITE = "#FFFFFF"
MUTED = "#8A7880"
GREEN = "#267A59"
LIGHT_PINK = "#F3E2E9"


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

# Testing helper: set AROVI_SHOW_ALL=1 in .env to skip the
# gender-preference filter in Discover. Remove it for real use.
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
# SUPABASE DATABASE FUNCTIONS
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
    """Does `preference` (who I'm interested in) accept `other_gender`?"""
    pref = norm(preference)

    if pref in ("", "everyone"):
        return True

    return pref == norm(gender_label(other_gender))


def preferences_match(first, second):
    # BUG FIX: the old version compared each person's preference with
    # their OWN gender, so a Man interested in Women never matched a
    # Woman interested in Men. Each side is now compared with the
    # OTHER person's gender.
    return (
        accepts(first.get("interested_in"), second.get("gender"))
        and accepts(second.get("interested_in"), first.get("gender"))
    )


def get_unseen_profiles(user_id):
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

    liked_ids = {
        str(row["receiver_id"]) for row in liked_rows
    }

    match_rows = (
        api_client.table("matches")
        .select("user_one,user_two")
        .or_(
            f"user_one.eq.{user_id},user_two.eq.{user_id}"
        )
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

    candidates = (
        api_client.table("profiles")
        .select("*")
        # a NULL is_active counts as active so older rows still appear
        .or_("is_active.is.null,is_active.eq.true")
        # suspended / banned members never appear in Discover
        .eq("status", "active")
        .neq("id", str(user_id))
        .order("created_at", desc=True)
        .execute()
        .data or []
    )

    excluded = liked_ids | matched_ids
    results = []

    for person in candidates:
        person_id = str(person.get("id", ""))

        try:
            age = int(person.get("age") or 0)
        except (ValueError, TypeError):
            age = 0

        if not person_id or person_id in excluded or age < 18:
            continue

        if SHOW_ALL or preferences_match(me, person):
            results.append(person)

    attach_avatars(results)

    return results


def get_matches(user_id):
    rows = (
        api().table("matches")
        .select("*")
        .or_(
            f"user_one.eq.{user_id},user_two.eq.{user_id}"
        )
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
    """Returns True if this like created a match. The match row is created by the database trigger from arovi_supabase_setup.sql (clients are not allowed to insert matches directly, so matches can't be faked)."""
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


# ============================================================
# REPORTS (user side)
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
# PROFILE PHOTOS (private Supabase Storage bucket + signed URLs)
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

# Flet renamed ImageFit -> BoxFit in newer versions; support both.
_BOX_FIT = getattr(ft, "BoxFit", None) or getattr(ft, "ImageFit")
FIT_COVER = _BOX_FIT.COVER
FIT_CONTAIN = _BOX_FIT.CONTAIN

# path -> (signed_url, expires_at_timestamp)
_url_cache = {}


def _full_url(url):
    if url and url.startswith("/"):
        return f"{SUPABASE_URL}/storage/v1{url}"
    return url


def signed_urls(paths):
    """Returns {path: signed_url}. One request for all uncached paths."""
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
    """Adds person["avatar_url"] (a temporary signed link) to each profile."""
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
        # don't leave an orphan file behind
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
# ADMIN DATABASE FUNCTIONS
# Every call goes through an admin_* SQL function that checks
# is_admin() on the server, so the app UI is not what protects data.
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


# ============================================================
# FLET APPLICATION
# ============================================================

def main(page: ft.Page):

    page.title = "AROVI - Meaningful Connections"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = BG
    page.padding = 0
    page.spacing = 0

    # photo picker (Flet 1.x uses page.services; older versions used overlay)
    file_picker = ft.FilePicker()

    try:
        page.services.append(file_picker)
    except AttributeError:
        page.overlay.append(file_picker)

    auth_user = get_auth_user()
    current_user_id = auth_user.id if auth_user else None

    current_tab = "Discover"
    active_notice = None

    # Discover queue, chat poller token, and which user the queue is for.
    # admin_mode = hidden admin login was unlocked on the login page
    # admin = this session is a verified admin using the dashboard
    state = {
        "queue": None,
        "index": 0,
        "chat_token": 0,
        "user": None,
        "admin_mode": False,
        "admin": False,
        "atab": "Overview",
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

    def primary_button(label, action):
        # Row-wrapped: expand=True then means full WIDTH (a bare expanded
        # button inside a Column stretches to full height)
        return ft.Row(
            controls=[
                ft.Button(
                    content=label,
                    on_click=action,
                    bgcolor=PRIMARY,
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

    def wide_button(label, action):
        # full-width button that is safe inside scrolling columns
        return ft.Row(
            controls=[
                ft.Button(
                    content=label,
                    on_click=action,
                    bgcolor=PRIMARY,
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
                size=25,
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
        return ft.Text(
            APP_NAME,
            size=size,
            weight=ft.FontWeight.BOLD,
            color=PRIMARY,
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
        """Full-screen photo viewer with previous / next."""

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
                                    on_click=lambda e: open_dashboard(),
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
        """Big photo with tappable thumbnails underneath."""

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

    def person_row(person, subtitle, on_tap):
        return ft.Container(
            bgcolor=WHITE,
            border_radius=18,
            padding=12,
            on_click=on_tap,
            content=ft.Row(
                spacing=12,
                controls=[
                    person_avatar(52, person),
                    ft.Column(
                        expand=True,
                        spacing=2,
                        controls=[
                            ft.Text(
                                display_name(person),
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
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
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=MUTED),
                ],
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

    def show_screen(content, navigation=False, nav=None):
        page.clean()
        page.bgcolor = BG
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
        elif navigation and current_user_id:
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
    # BOTTOM NAVIGATION
    # ========================================================

    def set_tab(tab):
        nonlocal current_tab

        current_tab = tab
        open_dashboard()

    def build_nav(tabs, selected_name, on_select):
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
                                    color=PRIMARY if selected else MUTED,
                                    size=20,
                                ),
                                ft.Text(
                                    tab_name,
                                    size=10,
                                    color=PRIMARY if selected else MUTED,
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
            bgcolor=WHITE,
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
    # SPLASH SCREEN
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
    # REGISTRATION
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

            nonlocal current_user_id, current_tab

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

                    current_user_id = result.user.id
                    current_tab = "Discover"

                    open_dashboard()

                else:

                    show_login()

                    notify(
                        "Account created. Check your email to confirm, "
                        "then sign in.",
                        GREEN,
                    )

            except Exception as ex:
                import traceback

                error_details = traceback.format_exc()

                print("AROVI REGISTRATION ERROR:")
                print(error_details)

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
    # LOGIN (tap the AROVI logo 3 times to unlock admin sign-in)
    # ========================================================

    def show_login(e=None):

        notice = make_notice()

        state["admin_mode"] = False
        taps = {"count": 0}

        admin_tag = ft.Text(
            "ADMIN SIGN-IN",
            size=11,
            weight=ft.FontWeight.BOLD,
            color=GOLD,
            visible=False,
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

                if state.get("admin_mode"):

                    if not profile.get("is_admin"):

                        api().auth.sign_out()
                        current_user_id = None
                        state["admin_mode"] = False

                        notify("This account is not authorized.")
                        return

                    state["admin"] = True
                    state["atab"] = "Overview"
                    open_admin()
                    return

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
    # DASHBOARD ROUTER
    # ========================================================

    def open_dashboard():

        nonlocal current_user_id

        # stops any running chat poller
        state["chat_token"] += 1

        if not current_user_id:
            show_welcome()
            return

        # verified admin sessions use the admin dashboard
        if state.get("admin"):
            open_admin()
            return

        # a different user signed in -> start with a fresh Discover queue
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
    # REPORT A MEMBER
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

        def submit(e):

            if not reason.value:
                notify("Choose a reason first.")
                return

            extra = (details.value or "").strip()
            text = reason.value + (f": {extra}" if extra else "")

            try:
                submit_report(current_user_id, person_id, text)
            except Exception as ex:
                notify(f"Could not send report: {ex}")
                return

            back()
            notify(
                "Thank you. Our team will review your report.",
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
                notice,
                wide_button("SEND REPORT", submit),
                secondary_button("CANCEL", lambda e: back()),
            ])
        )

    # ========================================================
    # DISCOVER
    # ========================================================

    def reload_discover(e=None):
        state["queue"] = None
        state["index"] = 0
        show_discover()

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
                        f"You and {name} liked each other.",
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
                        lambda e: open_dashboard(),
                    ),
                ],
            )
        )

    def show_discover():

        try:
            user = get_profile(current_user_id)

            if state["queue"] is None:
                state["queue"] = get_unseen_profiles(current_user_id)
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
                            icon=ft.Icons.REFRESH,
                            icon_color=PRIMARY,
                            tooltip="Refresh",
                            on_click=reload_discover,
                        ),
                    ],
                ),
            ],
        )

        # Explain WHY Discover is empty instead of a blank screen
        if not user.get("gender") or not user.get("interested_in"):

            show_screen(
                ft.Column(
                    expand=True,
                    spacing=14,
                    controls=[
                        header,
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
                        empty_view(
                            ft.Icons.EXPLORE_OUTLINED,
                            "No new profiles yet",
                            "You've seen everyone who fits your "
                            "preferences. Check back later when more "
                            "people join AROVI.",
                        ),
                        wide_button("REFRESH", reload_discover),
                    ],
                ),
                navigation=True,
            )
            return

        person = queue[idx]

        try:
            person_photos = get_photos(person["id"])
        except Exception:
            person_photos = []

        def like_person(e):

            try:
                mutual = create_like(
                    current_user_id,
                    person["id"],
                )
            except Exception as ex:
                notify(f"Could not send like: {ex}")
                return

            state["index"] += 1

            if mutual:
                show_match_screen(person)
            else:
                show_discover()
                notify(
                    "Like sent. If they like you back, you'll match.",
                    GREEN,
                )

        def skip_person(e):
            state["index"] += 1
            show_discover()

        def after_report():
            # move past the reported profile
            state["index"] += 1
            show_discover()

        card_controls = [
            gallery_view(person_photos, person),
            ft.Text(
                f"{display_name(person)}, {person.get('age', '')}",
                size=24,
                weight=ft.FontWeight.BOLD,
                color=DARK,
                text_align=ft.TextAlign.CENTER,
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

        actions = ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=28,
            controls=[
                ft.Container(
                    bgcolor=WHITE,
                    border_radius=40,
                    content=ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=32,
                        icon_color=MUTED,
                        tooltip="Pass",
                        on_click=skip_person,
                    ),
                ),
                ft.Container(
                    bgcolor=PRIMARY,
                    border_radius=40,
                    content=ft.IconButton(
                        icon=ft.Icons.FAVORITE,
                        icon_size=32,
                        icon_color=WHITE,
                        tooltip="Like",
                        on_click=like_person,
                    ),
                ),
            ],
        )

        report_row = ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            controls=[
                ft.TextButton(
                    content="Report this profile",
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
                heading(
                    "Discover",
                    f"{len(queue) - idx} people to meet",
                ),
                panel(
                    ft.Column(
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        ),
                        spacing=12,
                        controls=card_controls,
                    ),
                    padding=28,
                ),
                notice,
                actions,
                report_row,
            ]),
            navigation=True,
        )

    # ========================================================
    # MATCHES
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

        controls = [
            app_logo(25),
            heading(
                "Your matches",
                f"{len(matches)} mutual connection(s)",
            ),
        ]

        if not matches:

            controls.append(
                empty_view(
                    ft.Icons.FAVORITE_BORDER,
                    "No matches yet",
                    "Like profiles in Discover. When someone likes you "
                    "back, the match will appear here.",
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
                    )
                )

        show_screen(
            make_scrollable(controls),
            navigation=True,
        )

    # ========================================================
    # MESSAGES LIST
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

        # newest conversation first, empty ones last
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
    # PRIVATE CHAT
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
                                        ft.Text(
                                            display_name(person),
                                            weight=ft.FontWeight.BOLD,
                                            color=DARK,
                                        ),
                                        ft.Text(
                                            "AROVI match",
                                            size=11,
                                            color=GREEN,
                                        ),
                                    ],
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.FLAG_OUTLINED,
                                    icon_color=MUTED,
                                    tooltip="Report",
                                    on_click=lambda e: show_report(
                                        other_user_id,
                                        display_name(person),
                                        lambda: open_chat(
                                            other_user_id,
                                            origin_tab,
                                        ),
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

        # light polling so new messages appear without reopening the chat
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
    # PROFILE
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

                # preferences may have changed -> rebuild Discover
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

        # ---------- photos ----------
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
                    # ask the phone to send the image bytes (no file path on Android)
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
    # CHANGE PASSWORD (members and admins)
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

            # confirm the current password first
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
    # ADMIN DASHBOARD
    # Reached only through the hidden admin sign-in, and only when
    # the database says is_admin = true for the signed-in account.
    # ========================================================

    ADMIN_PAGE = 20

    def set_atab(tab):
        state["atab"] = tab
        open_admin()

    def admin_nav():
        return build_nav(
            [
                ("Overview", ft.Icons.DASHBOARD_OUTLINED),
                ("Users", ft.Icons.PEOPLE_OUTLINE),
                ("Photos", ft.Icons.PHOTO_LIBRARY_OUTLINED),
                ("Reports", ft.Icons.FLAG_OUTLINED),
            ],
            state.get("atab", "Overview"),
            set_atab,
        )

    def admin_screen(content):
        show_screen(content, nav=admin_nav())

    def admin_error(title, ex, retry):
        show_screen(error_view(title, ex, retry), nav=admin_nav())

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
            atab="Overview",
            asearch="",
            afilter="all",
            apage=0,
            aphoto_page=0,
            areport="open",
        )

        show_welcome()

    def admin_header(title, subtitle=None):
        return ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Column(
                    expand=True,
                    spacing=4,
                    controls=[
                        ft.Text(
                            "AROVI ADMIN",
                            size=11,
                            weight=ft.FontWeight.BOLD,
                            color=GOLD,
                        ),
                        heading(title, subtitle),
                    ],
                ),
                ft.Row(
                    spacing=0,
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.LOCK_RESET,
                            icon_color=PRIMARY,
                            tooltip="Change password",
                            on_click=lambda e: show_change_password(
                                open_admin
                            ),
                        ),
                        ft.IconButton(
                            icon=ft.Icons.LOGOUT,
                            icon_color=PRIMARY,
                            tooltip="Sign out",
                            on_click=admin_logout,
                        ),
                    ],
                ),
            ],
        )

    def admin_run(action, ok_message, after):
        """Run an admin action, rebuild the screen, then show the result."""
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
                    wide_button(yes_label, lambda e: on_yes()),
                    secondary_button("CANCEL", lambda e: back()),
                ],
            ),
            nav=admin_nav(),
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
            bgcolor=PRIMARY if selected else WHITE,
            border_radius=20,
            padding=ft.Padding(left=14, top=7, right=14, bottom=7),
            on_click=on_tap,
            content=ft.Text(
                label,
                size=12,
                weight=ft.FontWeight.BOLD,
                color=WHITE if selected else DARK,
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
        tags = [(norm(person.get("status")) or "active").title()]

        if person.get("is_admin"):
            tags.append("Admin")

        if person.get("is_active") is False:
            tags.append("Hidden")

        parts = [" / ".join(tags)]
        extra = person_subtitle(person)

        if extra:
            parts.append(extra)

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

        tab = state.get("atab", "Overview")

        if tab == "Users":
            admin_users()
        elif tab == "Photos":
            admin_photos()
        elif tab == "Reports":
            admin_reports()
        else:
            admin_overview()

    # ---------- overview ----------

    def admin_overview():

        try:
            stats = admin_stats()
            recent = admin_list_users("", "all", 0, 5)
        except Exception as ex:
            admin_error("Could not load overview", ex, admin_overview)
            return

        items = [
            ("Members", "members"),
            ("New today", "new_today"),
            ("New this week", "new_week"),
            ("Active", "active"),
            ("Suspended", "suspended"),
            ("Banned", "banned"),
            ("Hidden", "hidden"),
            ("Admins", "admins"),
            ("Photos", "photos"),
            ("Likes", "likes"),
            ("Matches", "matches"),
            ("Messages", "messages"),
            ("Open reports", "open_reports"),
        ]

        tiles = [
            ft.Container(
                width=150,
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
                            color=PRIMARY,
                        ),
                        ft.Text(label, size=12, color=MUTED),
                    ],
                ),
            )
            for label, key in items
        ]

        controls = [
            admin_header("Overview", "How AROVI is doing right now."),
            ft.Row(
                wrap=True,
                spacing=10,
                run_spacing=10,
                controls=tiles,
            ),
            ft.Text(
                "Newest members",
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
                        admin_overview,
                    ),
                )
            )

        controls.append(make_notice())
        controls.append(wide_button("REFRESH", lambda e: admin_overview()))

        admin_screen(make_scrollable(controls))

    # ---------- users ----------

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

        search_field = ft.TextField(
            hint_text="Search name, email or city (press Enter)",
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
            ("Admins", "admins"),
            ("Hidden", "hidden"),
        ]

        controls = [
            admin_header(
                "Users",
                f"{stats.get('members', 0)} members in total",
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
            make_notice(),
        ]

        if not rows:
            controls.append(
                empty_view(
                    ft.Icons.PEOPLE_OUTLINE,
                    "No users found",
                    "Try a different search or filter.",
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
                    info("Email", person.get("email")),
                    info("Age", person.get("age")),
                    info("Gender", person.get("gender")),
                    info("Interested in", person.get("interested_in")),
                    info("Location", person.get("location")),
                    info("Status", status.title()),
                    info("Visible in Discover", "Yes" if visible_now else "No"),
                    info("Joined", str(person.get("created_at") or "")[:10]),
                    ft.Text(
                        (person.get("bio") or "No bio.").strip(),
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
                        "Activity",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    info("Likes sent", summary.get("likes_sent", 0)),
                    info("Likes received", summary.get("likes_received", 0)),
                    info("Matches", summary.get("matches", 0)),
                    info("Messages sent", summary.get("messages_sent", 0)),
                    info("Reports against", summary.get("reports_against", 0)),
                    info("Reports filed", summary.get("reports_filed", 0)),
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
                        content="Remove",
                        on_click=lambda e, p=photo: admin_confirm(
                            "Remove this photo?",
                            "The photo is deleted for everyone.",
                            "REMOVE PHOTO",
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
                        f"Photos ({len(photos)})",
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
                        else ft.Text("No photos.", size=13, color=MUTED)
                    ),
                ],
            )
        )

        match_controls = [
            ft.Text(
                f"Matches ({len(matches)})",
                size=16,
                weight=ft.FontWeight.BOLD,
                color=DARK,
            )
        ]

        if not matches:
            match_controls.append(
                ft.Text("No matches.", size=13, color=MUTED)
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
                            content="Unmatch",
                            on_click=lambda e, mid=m["match_id"], n=m.get("other_name"): admin_confirm(
                                "Unmatch these members?",
                                f"{name} and {n or 'this member'} will no "
                                "longer be matched and their chat is deleted.",
                                "UNMATCH",
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

        actions = []

        if is_self:
            actions.append(
                ft.Text(
                    "This is your own admin account.",
                    size=12,
                    color=MUTED,
                )
            )

        elif target_is_admin:
            actions.append(
                outline_wide(
                    "REMOVE ADMIN ROLE",
                    lambda e: admin_confirm(
                        f"Remove admin role from {name}?",
                        "They become a normal member.",
                        "REMOVE ADMIN",
                        lambda: admin_run(
                            lambda: admin_set_admin(user_id, False),
                            "Admin role removed.",
                            refresh,
                        ),
                        refresh,
                    ),
                )
            )

        else:
            if status == "active":
                actions.append(
                    wide_button(
                        "SUSPEND",
                        lambda e: admin_run(
                            lambda: admin_set_status(user_id, "suspended"),
                            "User suspended.",
                            refresh,
                        ),
                    )
                )
                actions.append(
                    wide_button(
                        "BAN",
                        lambda e: admin_confirm(
                            f"Ban {name}?",
                            "They will be signed out and unable to sign in.",
                            "BAN USER",
                            lambda: admin_run(
                                lambda: admin_set_status(user_id, "banned"),
                                "User banned.",
                                refresh,
                            ),
                            refresh,
                        ),
                    )
                )
            else:
                actions.append(
                    wide_button(
                        "REACTIVATE",
                        lambda e: admin_run(
                            lambda: admin_set_status(user_id, "active"),
                            "User reactivated.",
                            refresh,
                        ),
                    )
                )

            actions.append(
                outline_wide(
                    "HIDE FROM DISCOVER" if visible_now else "SHOW IN DISCOVER",
                    lambda e: admin_run(
                        lambda: admin_set_visibility(user_id, not visible_now),
                        "Visibility updated.",
                        refresh,
                    ),
                )
            )

            if (person.get("bio") or "").strip():
                actions.append(
                    outline_wide(
                        "CLEAR BIO",
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
                    "MAKE ADMIN",
                    lambda e: admin_confirm(
                        f"Make {name} an admin?",
                        "They will be able to manage all members. "
                        "Only do this for people you trust.",
                        "MAKE ADMIN",
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
                outline_wide(
                    "DELETE ACCOUNT",
                    lambda e: admin_confirm(
                        f"Delete {name}?",
                        "This permanently deletes the account, photos, "
                        "likes, matches and messages. It cannot be undone.",
                        "DELETE FOREVER",
                        lambda: admin_run(
                            lambda: admin_delete_user(user_id),
                            "User deleted.",
                            admin_users,
                        ),
                        refresh,
                    ),
                    icon=ft.Icons.DELETE_FOREVER,
                )
            )

        admin_screen(
            make_scrollable([
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.ARROW_BACK,
                            icon_color=PRIMARY,
                            on_click=lambda e: back(),
                        ),
                        person_avatar(52, person),
                        ft.Column(
                            expand=True,
                            spacing=2,
                            controls=[
                                ft.Text(
                                    name,
                                    size=18,
                                    weight=ft.FontWeight.BOLD,
                                    color=DARK,
                                ),
                                ft.Text(
                                    status.title()
                                    + (" \u2022 Admin" if target_is_admin else ""),
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
                *actions,
            ])
        )

    # ---------- photo moderation ----------

    def admin_photos(page=None):

        if page is not None:
            state["aphoto_page"] = max(0, page)

        page_number = state.get("aphoto_page", 0)
        size = 12

        try:
            rows = admin_recent_photos(page_number * size, size + 1)
            has_next = len(rows) > size
            rows = rows[:size]
            urls = signed_urls([r["path"] for r in rows]) if rows else {}
        except Exception as ex:
            admin_error("Could not load photos", ex, admin_photos)
            return

        def remove(photo):
            admin_run(
                lambda: admin_remove_photo(photo),
                "Photo removed.",
                admin_photos,
            )

        tiles = []

        for photo in rows:

            url = urls.get(photo["path"])

            if not url:
                continue

            tiles.append(
                ft.Column(
                    spacing=0,
                    width=150,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            width=150,
                            height=150,
                            border_radius=12,
                            bgcolor=LIGHT_PINK,
                            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                            on_click=lambda e, u=url: show_photo_viewer([u], 0),
                            content=ft.Image(
                                src=url,
                                width=150,
                                height=150,
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
                                    content="Profile",
                                    on_click=lambda e, uid=photo["user_id"]: admin_user_detail(
                                        uid,
                                        admin_photos,
                                    ),
                                ),
                                ft.TextButton(
                                    content="Remove",
                                    on_click=lambda e, p=photo: admin_confirm(
                                        "Remove this photo?",
                                        "The photo is deleted for everyone.",
                                        "REMOVE PHOTO",
                                        lambda: remove(p),
                                        admin_photos,
                                    ),
                                ),
                            ],
                        ),
                    ],
                )
            )

        controls = [
            admin_header("Photos", "Newest uploads first. Review and remove."),
            make_notice(),
        ]

        if tiles:
            controls.append(
                ft.Row(
                    wrap=True,
                    spacing=10,
                    run_spacing=10,
                    alignment=ft.MainAxisAlignment.CENTER,
                    controls=tiles,
                )
            )
        else:
            controls.append(
                empty_view(
                    ft.Icons.PHOTO_LIBRARY_OUTLINED,
                    "No photos yet",
                    "Uploaded profile photos will appear here.",
                )
            )

        if page_number > 0 or has_next:
            controls.append(
                pager(
                    page_number,
                    has_next,
                    lambda n: admin_photos(page=n),
                )
            )

        admin_screen(make_scrollable(controls))

    # ---------- reports ----------

    def admin_view_chat(first_id, second_id, names):

        try:
            messages = admin_get_conversation(first_id, second_id)
        except Exception as ex:
            admin_error("Could not load conversation", ex, admin_reports)
            return

        controls = [
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        icon_color=PRIMARY,
                        on_click=lambda e: admin_reports(),
                    ),
                    ft.Text(
                        "Reported conversation",
                        size=18,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                ]
            ),
            ft.Text(
                "Shown only because of a report. Private messages "
                "are confidential.",
                size=12,
                color=MUTED,
            ),
        ]

        if not messages:
            controls.append(
                ft.Text("No messages between these members.", color=MUTED)
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

    def admin_reports(which=None):

        if which is not None:
            state["areport"] = which

        mode = state.get("areport", "open")

        try:
            reports = admin_list_reports(mode)
        except Exception as ex:
            admin_error("Could not load reports", ex, admin_reports)
            return

        names = {}

        for r in reports:
            names[str(r["reporter_id"])] = r.get("reporter_name") or "Member"
            names[str(r["reported_id"])] = r.get("reported_name") or "Member"

        def dismiss(report):
            admin_run(
                lambda: admin_resolve_report(report["id"], "dismissed"),
                "Report dismissed.",
                admin_reports,
            )

        def sanction(report, status):
            def action():
                admin_set_status(report["reported_id"], status)
                admin_resolve_report(report["id"], "resolved")

            admin_run(
                action,
                f"User {status} and report resolved.",
                admin_reports,
            )

        controls = [
            admin_header(
                "Reports",
                f"{len(reports)} {'open' if mode == 'open' else 'closed'}",
            ),
            ft.Row(
                spacing=8,
                controls=[
                    chip("Open", mode == "open", lambda e: admin_reports("open")),
                    chip("Closed", mode != "open", lambda e: admin_reports("closed")),
                ],
            ),
            make_notice(),
        ]

        if not reports:
            controls.append(
                empty_view(
                    ft.Icons.FLAG_OUTLINED,
                    "No reports here",
                    "New reports from members will appear in Open.",
                )
            )

        for report in reports:

            target = str(report["reported_id"])
            reporter = str(report["reporter_id"])
            is_open = report.get("status") == "open"

            buttons = [
                ft.TextButton(
                    content="Profile",
                    on_click=lambda e, t=target: admin_user_detail(
                        t,
                        admin_reports,
                    ),
                ),
                ft.TextButton(
                    content="Chat",
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
                            "They will be banned and this report "
                            "marked resolved.",
                            "BAN USER",
                            lambda: sanction(r, "banned"),
                            admin_reports,
                        ),
                    ),
                ]

            controls.append(
                panel(
                    ft.Column(
                        spacing=6,
                        controls=[
                            ft.Text(
                                f"{names.get(target, 'Member')} was reported",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            ft.Text(
                                f"By {names.get(reporter, 'Member')} \u2022 "
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