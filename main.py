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
# Complete single-file application
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

        if person:
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
    """Returns True if this like created a match.

    The match row is created by the database trigger from
    arovi_supabase_setup.sql (clients are not allowed to insert
    matches directly, so matches can't be faked)."""
    sender_id = str(sender_id)
    receiver_id = str(receiver_id)

    if sender_id == receiver_id:
        return False

    receiver = get_profile(receiver_id)

    if not receiver or receiver.get("is_active") is False:
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

    # Discover queue, chat poller token, and which user the queue is for
    state = {"queue": None, "index": 0, "chat_token": 0, "user": None}

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
        return ft.Button(
            content=label,
            on_click=action,
            bgcolor=PRIMARY,
            color=WHITE,
            height=48,
            expand=True,
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

        return "  •  ".join(parts)

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

    def show_screen(content, navigation=False):
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

        if navigation and current_user_id:
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

    def build_bottom_navigation():

        tabs = [
            ("Discover", ft.Icons.EXPLORE_OUTLINED),
            ("Matches", ft.Icons.FAVORITE_BORDER),
            ("Messages", ft.Icons.CHAT_BUBBLE_OUTLINE),
            ("Profile", ft.Icons.PERSON_OUTLINE),
        ]

        buttons = []

        for tab_name, tab_icon in tabs:

            selected = current_tab == tab_name

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
                        on_click=lambda e, t=tab_name: set_tab(t),
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
                    "18+ ONLY  •  RESPECT  •  HONEST CONNECTIONS",
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
    # LOGIN
    # ========================================================

    def show_login(e=None):

        notice = make_notice()

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

                current_tab = "Discover"
                open_dashboard()

            except Exception as ex:
                notify(f"Sign-in failed: {ex}")

        content = ft.Column(
            expand=True,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=15,
            controls=[
                app_logo(34),
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
                    preview = "Say hello 👋"

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
                            "You matched! Say hello 👋",
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
            state.update(queue=None, index=0, user=None)

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

            try:
                picked = await file_picker.pick_files(
                    dialog_title="Choose photos",
                    file_type=ft.FilePickerFileType.IMAGE,
                    allow_multiple=True,
                )
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

                    if not data:
                        with open(f.path, "rb") as handle:
                            data = handle.read()

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
                content="LOG OUT",
                icon=ft.Icons.LOGOUT,
                on_click=logout,
            ),
            ft.Text(
                "AROVI • 18+ COMMUNITY",
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
