import os
import re
import uuid
import asyncio
from pathlib import Path
from datetime import datetime, timezone, date

import flet as ft
from dotenv import load_dotenv
from supabase import create_client


# ============================================================
# AROVI - ADULT MATCHMAKING APPLICATION
# Flet 1.1.2 + Supabase + built-in admin side
# Complete single-file application
# ============================================================

APP_NAME = "AROVI"
APP_VERSION = "1.0"

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

SCHEMA = (os.getenv("AROVI_SCHEMA", "public") or "public").strip()
PHOTO_BUCKET = (os.getenv("AROVI_BUCKET", "arovi-photos") or "arovi-photos").strip()
SUPPORT_EMAIL = (
    os.getenv("AROVI_SUPPORT_EMAIL", "support@arovi.app") or "support@arovi.app"
).strip()

config_error = None

if not (SUPABASE_URL and SUPABASE_KEY):
    config_error = "Supabase URL or publishable key is missing."


def make_client():
    # One client PER visitor session. A single shared client would share
    # the signed-in user between everyone who opens the website.
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# SMALL HELPERS
# ============================================================

def norm(value):
    return (value or "").strip().lower()


def is_blocked(profile):
    """True when an admin has suspended or banned this account."""
    return norm((profile or {}).get("status")) in ("suspended", "banned")


def calc_age(born):
    """Age in years from a YYYY-MM-DD date of birth (or None)."""
    if not born:
        return None

    try:
        b = date.fromisoformat(str(born)[:10])
    except ValueError:
        return None

    today = date.today()

    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def friendly(ex):
    """Readable text from a Supabase / network exception."""
    message = getattr(ex, "message", None) or ""

    if not message and getattr(ex, "args", None):
        first = ex.args[0]

        if isinstance(first, dict):
            message = first.get("message") or ""

    return (message or str(ex)).strip()


REPORT_REASONS = [
    "Fake profile or scam",
    "Harassment or abusive messages",
    "Inappropriate photos",
    "Under 18",
    "Spam or advertising",
    "Other",
]

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


# ============================================================
# DATA LAYER (one instance per visitor session)
# Every sensitive action is a server-side function in the database
# (arovi_setup.sql), so the app cannot bypass the rules.
# ============================================================

class AroviData:

    def __init__(self, client):
        self.client = client
        self._urls = {}

    # ---------- plumbing ----------

    def db(self):
        if SCHEMA == "public":
            return self.client

        try:
            return self.client.schema(SCHEMA)
        except AttributeError:
            return self.client.postgrest.schema(SCHEMA)

    def rpc(self, name, params=None):
        return self.db().rpc(name, params or {}).execute().data

    def auth_email(self):
        try:
            result = self.client.auth.get_user()
            return getattr(result.user, "email", "") or ""
        except Exception:
            return ""

    @staticmethod
    def with_age(row):
        if "birth_date" in row:
            row["age"] = calc_age(row.get("birth_date"))
        return row

    # ---------- photos / signed links ----------

    def signed_urls(self, paths):
        """Returns {path: signed_url}. One request for all uncached paths."""
        now = datetime.now(timezone.utc).timestamp()
        wanted = [p for p in dict.fromkeys(paths) if p]

        missing = [
            p for p in wanted
            if p not in self._urls or self._urls[p][1] < now + 300
        ]

        if missing:
            items = (
                self.client.storage.from_(PHOTO_BUCKET)
                .create_signed_urls(missing, SIGNED_URL_SECONDS)
            )

            for item in items or []:
                file_path = item.get("path")
                url = item.get("signedURL") or item.get("signedUrl")

                if file_path and url and not item.get("error"):
                    if url.startswith("/"):
                        url = f"{SUPABASE_URL}/storage/v1{url}"

                    self._urls[file_path] = (url, now + SIGNED_URL_SECONDS)

        return {p: self._urls[p][0] for p in wanted if p in self._urls}

    def attach_avatars(self, people):
        """Adds person["avatar_url"] (a temporary signed link) to each row."""
        paths = [p.get("avatar_path") for p in people if p.get("avatar_path")]

        try:
            urls = self.signed_urls(paths) if paths else {}
        except Exception:
            urls = {}

        for person in people:
            person["avatar_url"] = urls.get(person.get("avatar_path"))

        return people

    def get_photos(self, user_id):
        rows = (
            self.db().table("profile_photos")
            .select("*")
            .eq("user_id", str(user_id))
            .order("created_at")
            .execute()
            .data or []
        )

        if not rows:
            return []

        urls = self.signed_urls([r["path"] for r in rows])

        if not urls:
            raise RuntimeError(
                "Could not create photo links. Run arovi_setup.sql in "
                "Supabase and try again."
            )

        for row in rows:
            row["url"] = urls.get(row["path"])

        return [r for r in rows if r.get("url")]

    def set_main_photo(self, user_id, file_path):
        self.update_profile(user_id, {"avatar_path": file_path})

    def upload_photo(self, user_id, filename, data):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content_type = IMAGE_TYPES.get(ext)

        if not content_type:
            raise ValueError("Use a JPG, PNG or WEBP image.")

        if not data or len(data) > MAX_PHOTO_BYTES:
            raise ValueError("Each photo must be 5 MB or smaller.")

        if len(self.get_photos(user_id)) >= MAX_PHOTOS:
            raise ValueError(f"You can upload up to {MAX_PHOTOS} photos.")

        file_path = f"{user_id}/{uuid.uuid4().hex}.{ext}"
        storage = self.client.storage.from_(PHOTO_BUCKET)

        storage.upload(file_path, data, {"content-type": content_type})

        try:
            self.db().table("profile_photos").insert({
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

        profile = self.get_profile(user_id)

        if profile and not profile.get("avatar_path"):
            self.set_main_photo(user_id, file_path)

        return file_path

    def delete_photo(self, user_id, photo):
        try:
            self.client.storage.from_(PHOTO_BUCKET).remove([photo["path"]])
        except Exception:
            pass

        (
            self.db().table("profile_photos")
            .delete()
            .eq("id", photo["id"])
            .eq("user_id", str(user_id))
            .execute()
        )

        self._urls.pop(photo["path"], None)

        profile = self.get_profile(user_id)

        if profile and profile.get("avatar_path") == photo["path"]:
            remaining = self.get_photos(user_id)
            self.set_main_photo(
                user_id,
                remaining[0]["path"] if remaining else None,
            )

    # ---------- own profile ----------

    def get_profile(self, user_id):
        if not user_id:
            return None

        rows = (
            self.db().table("profiles")
            .select("*")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
            .data or []
        )

        if not rows:
            return None

        row = self.with_age(rows[0])
        self.attach_avatars([row])

        return row

    def update_profile(self, user_id, fields):
        (
            self.db().table("profiles")
            .update(fields)
            .eq("id", str(user_id))
            .execute()
        )

    def create_profile(self, name, born, gender, interested_in, location):
        self.rpc("create_my_profile", {
            "p_name": name,
            "p_birth_date": born.isoformat(),
            "p_gender": gender,
            "p_interested_in": interested_in,
            "p_location": location,
        })

    def delete_account(self, user_id):
        photo_rows = (
            self.db().table("profile_photos")
            .select("path")
            .eq("user_id", str(user_id))
            .execute()
            .data or []
        )

        paths = [r["path"] for r in photo_rows if r.get("path")]

        self.rpc("delete_my_account")

        if paths:
            try:
                self.client.storage.from_(PHOTO_BUCKET).remove(paths)
            except Exception:
                pass

    # ---------- discover / matches / chat ----------

    def get_discover(self):
        rows = self.rpc("discover", {"p_limit": 40}) or []
        self.attach_avatars(rows)
        return rows

    def like_person(self, target):
        return bool(self.rpc("like_user", {"target": str(target)}))

    def pass_person(self, target):
        self.rpc("pass_user", {"target": str(target)})

    def unmatch_person(self, target):
        self.rpc("unmatch_user", {"target": str(target)})

    def block_person(self, target):
        self.rpc("block_user", {"target": str(target)})

    def unblock_person(self, target):
        self.rpc("unblock_user", {"target": str(target)})

    def get_blocked(self):
        rows = self.rpc("my_blocked") or []
        self.attach_avatars(rows)
        return rows

    def get_matches(self):
        rows = self.rpc("my_matches") or []
        self.attach_avatars(rows)
        return rows

    def get_likes_received(self):
        rows = self.rpc("likes_received") or []
        self.attach_avatars(rows)
        return rows

    def get_conversation(self, me, other):
        return (
            self.db().table("messages")
            .select("*")
            .or_(
                f"and(sender_id.eq.{me},receiver_id.eq.{other}),"
                f"and(sender_id.eq.{other},receiver_id.eq.{me})"
            )
            .order("created_at")
            .execute()
            .data or []
        )

    def send_message(self, other, body):
        self.rpc("send_message", {"target": str(other), "body": body})

    def mark_read(self, other):
        self.rpc("mark_read", {"other": str(other)})

    def unread_total(self):
        try:
            return int(self.rpc("unread_total") or 0)
        except (TypeError, ValueError):
            return 0

    def report_user(self, target, reason):
        self.rpc("report_user", {
            "target": str(target),
            "reason": (reason or "").strip()[:1000],
        })

    def get_announcement(self):
        rows = (
            self.db().table("announcements")
            .select("*")
            .eq("active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data or []
        )

        return rows[0] if rows else None

    # ---------- admin ----------

    def check_admin(self, user_id):
        rows = (
            self.db().table("profiles")
            .select("is_admin,status")
            .eq("id", str(user_id))
            .limit(1)
            .execute()
            .data or []
        )

        return bool(
            rows
            and rows[0].get("is_admin")
            and norm(rows[0].get("status")) == "active"
        )

    def admin_stats(self):
        data = self.rpc("admin_stats")
        return data if isinstance(data, dict) else {}

    def admin_list_users(self, search="", filter_by="all", skip=0, limit=20):
        rows = self.rpc("admin_list_users", {
            "search_text": (search or "").strip(),
            "filter_by": filter_by,
            "skip_rows": int(skip),
            "page_size": int(limit),
        }) or []

        for row in rows:
            self.with_age(row)

        self.attach_avatars(rows)

        return rows

    def admin_user_summary(self, user_id):
        data = self.rpc("admin_user_summary", {"target": str(user_id)})
        return data if isinstance(data, dict) else {}

    def admin_user_matches(self, user_id):
        return self.rpc("admin_user_matches", {"target": str(user_id)}) or []

    def admin_unmatch(self, match_id):
        self.rpc("admin_unmatch", {"match_id": str(match_id)})

    def admin_set_status(self, user_id, status):
        self.rpc("admin_set_status", {
            "target": str(user_id),
            "new_status": status,
        })

    def admin_set_visibility(self, user_id, visible):
        self.rpc("admin_set_visibility", {
            "target": str(user_id),
            "visible": bool(visible),
        })

    def admin_set_admin(self, user_id, make_admin):
        self.rpc("admin_set_admin", {
            "target": str(user_id),
            "make_admin": bool(make_admin),
        })

    def admin_clear_bio(self, user_id):
        self.rpc("admin_clear_bio", {"target": str(user_id)})

    def admin_delete_user(self, user_id):
        photo_rows = (
            self.db().table("profile_photos")
            .select("path")
            .eq("user_id", str(user_id))
            .execute()
            .data or []
        )

        paths = [r["path"] for r in photo_rows if r.get("path")]

        self.rpc("admin_delete_user", {"target": str(user_id)})

        if paths:
            try:
                self.client.storage.from_(PHOTO_BUCKET).remove(paths)
            except Exception:
                pass

    def admin_recent_photos(self, skip=0, limit=12):
        return self.rpc("admin_recent_photos", {
            "skip_rows": int(skip),
            "page_size": int(limit),
        }) or []

    def admin_remove_photo(self, photo):
        self.rpc("admin_remove_photo", {"photo_id": str(photo["id"])})

        try:
            self.client.storage.from_(PHOTO_BUCKET).remove([photo["path"]])
        except Exception:
            pass

        self._urls.pop(photo["path"], None)

    def admin_list_reports(self, filter_by="open"):
        return self.rpc("admin_list_reports", {"filter_by": filter_by}) or []

    def admin_resolve_report(self, report_id, status):
        self.rpc("admin_resolve_report", {
            "rid": str(report_id),
            "new_status": status,
        })

    def admin_get_conversation(self, first_id, second_id):
        return self.rpc("admin_get_conversation", {
            "a": str(first_id),
            "b": str(second_id),
        }) or []

    def admin_list_announcements(self):
        return self.rpc("admin_list_announcements") or []

    def admin_post_announcement(self, title, body):
        self.rpc("admin_post_announcement", {
            "p_title": title,
            "p_body": body,
        })

    def admin_set_announcement_active(self, announcement_id, active):
        self.rpc("admin_set_announcement_active", {
            "aid": str(announcement_id),
            "is_on": bool(active),
        })

    def admin_delete_announcement(self, announcement_id):
        self.rpc("admin_delete_announcement", {"aid": str(announcement_id)})

    def admin_list_log(self, skip=0, limit=25):
        return self.rpc("admin_list_log", {
            "skip_rows": int(skip),
            "page_size": int(limit),
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

    boot_error = config_error
    d = None

    if not boot_error:
        try:
            d = AroviData(make_client())
        except Exception as exc:
            boot_error = f"Supabase initialization failed: {exc}"

    # photo picker (Flet 1.x uses page.services; older versions used overlay)
    file_picker = ft.FilePicker()

    try:
        page.services.append(file_picker)
    except AttributeError:
        page.overlay.append(file_picker)

    # every visitor signs in themselves (no shared login)
    current_user_id = None
    current_tab = "Discover"
    active_notice = None

    # queue/index = Discover cards; chat_token stops chat polling;
    # admin_mode = hidden admin login unlocked; admin = verified admin session
    state = {
        "queue": None,
        "index": 0,
        "chat_token": 0,
        "user": None,
        "matches": None,
        "unread": 0,
        "announcement": None,
        "dismissed": None,
        "admin_mode": False,
        "admin": False,
        "atab": "Overview",
        "asearch": "",
        "afilter": "all",
        "apage": 0,
        "aphoto_page": 0,
        "areport": "open",
        "alog_page": 0,
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

    def person_row(person, subtitle, on_tap, trailing=None):
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
                    trailing or ft.Icon(ft.Icons.CHEVRON_RIGHT, color=MUTED),
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
    # SHARED HELPERS
    # ========================================================

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

    def unread_pill(count):
        return ft.Container(
            bgcolor=PRIMARY,
            border_radius=12,
            padding=ft.Padding(left=8, top=2, right=8, bottom=2),
            content=ft.Text(
                str(count),
                size=11,
                weight=ft.FontWeight.BOLD,
                color=WHITE,
            ),
        )

    def refresh_unread():
        try:
            state["unread"] = d.unread_total()
        except Exception:
            pass

    def sign_out_all(to="welcome"):

        nonlocal current_user_id, current_tab

        try:
            d.client.auth.sign_out()
        except Exception:
            pass

        current_user_id = None
        current_tab = "Discover"

        state["chat_token"] += 1
        state.update(
            queue=None,
            index=0,
            user=None,
            matches=None,
            unread=0,
            announcement=None,
            admin=False,
            admin_mode=False,
            atab="Overview",
            asearch="",
            afilter="all",
            apage=0,
            aphoto_page=0,
            areport="open",
            alog_page=0,
        )

        if to == "login":
            show_login()
        else:
            show_welcome()

    def confirm_view(title, message, yes_label, on_yes, back):

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
                    make_notice(),
                    wide_button(yes_label, lambda e: on_yes()),
                    secondary_button("CANCEL", lambda e: back()),
                ],
            ),
            navigation=True,
        )

    def gender_dropdown(label="I identify as", value=None):
        return ft.Dropdown(
            label=label,
            value=value or None,
            options=[
                ft.dropdown.Option("Man"),
                ft.dropdown.Option("Woman"),
                ft.dropdown.Option("Non-binary"),
            ],
        )

    def interest_dropdown(label="I am interested in", value=None):
        return ft.Dropdown(
            label=label,
            value=value or None,
            options=[
                ft.dropdown.Option("Men"),
                ft.dropdown.Option("Women"),
                ft.dropdown.Option("Non-binary"),
                ft.dropdown.Option("Everyone"),
            ],
        )

    def read_birth_date(text):
        """Returns (date, None) or (None, error message)."""
        try:
            born = datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            return None, (
                "Enter your date of birth as YYYY-MM-DD, "
                "for example 1995-08-24."
            )

        age = calc_age(born.isoformat())

        if age is None or age < 18:
            return None, "AROVI is only for adults aged 18 and above."

        if age > 100:
            return None, "Enter a valid date of birth."

        return born, None

    # ========================================================
    # BOTTOM NAVIGATION
    # ========================================================

    def set_tab(tab):
        nonlocal current_tab

        current_tab = tab
        open_dashboard()

    def build_nav(tabs, selected_name, on_select, badges=None):
        badges = badges or {}
        buttons = []

        for tab_name, tab_icon in tabs:

            selected = selected_name == tab_name
            badge = badges.get(tab_name)
            label = f"{tab_name} ({badge})" if badge else tab_name

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
                                    label,
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
            badges={"Messages": state.get("unread", 0)},
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
                ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    wrap=True,
                    spacing=0,
                    controls=[
                        ft.TextButton(
                            content="Terms",
                            on_click=lambda e: show_terms(show_welcome),
                        ),
                        ft.TextButton(
                            content="Privacy",
                            on_click=lambda e: show_privacy(show_welcome),
                        ),
                        ft.TextButton(
                            content="Safety tips",
                            on_click=lambda e: show_safety(show_welcome),
                        ),
                    ],
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
    # REGISTRATION (after sign-up the member goes to the LOGIN screen)
    # ========================================================

    def show_register(e=None):

        notice = make_notice()

        name = field("Full name")

        email = field(
            "Email address",
            keyboard=ft.KeyboardType.EMAIL,
        )

        born = field("Date of birth (YYYY-MM-DD)")

        gender = gender_dropdown()
        interested = interest_dropdown()

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
                "I confirm that I am 18 or older and agree to the "
                "Terms and Privacy Policy."
            )
        )

        def register(e):

            full_name = (name.value or "").strip()
            mail = (email.value or "").strip().lower()
            place = (location.value or "").strip()

            secret = password.value or ""
            confirmation = confirm.value or ""

            if not full_name or not mail or not place:
                notify("Please complete all required fields.")
                return

            if not re.match(
                r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                mail,
            ):
                notify("Enter a valid email address.")
                return

            birth, problem = read_birth_date(born.value)

            if problem:
                notify(problem)
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
                notify("Please confirm the 18+ requirement and the terms.")
                return

            try:

                result = d.client.auth.sign_up({
                    "email": mail,
                    "password": secret,
                    "options": {
                        "data": {
                            "app": "arovi",
                            "name": full_name,
                            "birth_date": birth.isoformat(),
                            "gender": gender.value,
                            "interested_in": interested.value,
                            "location": place,
                        }
                    },
                })

            except Exception as ex:

                text = friendly(ex)

                if "already" in text.lower():
                    notify(
                        "This email already has an account. Sign in "
                        "instead (if it was made in another app you will "
                        "be asked to finish your AROVI profile)."
                    )
                else:
                    notify(f"Registration error: {text}")

                return

            user = getattr(result, "user", None)

            if user is None:
                notify("Registration could not be completed.")
                return

            identities = getattr(user, "identities", None)

            if identities is not None and len(identities) == 0:
                notify(
                    "This email already has an account. Sign in instead "
                    "(if it was made in another app you will be asked to "
                    "finish your AROVI profile)."
                )
                return

            had_session = bool(getattr(result, "session", None))

            # never keep the new member signed in - they go to the login page
            try:
                d.client.auth.sign_out()
            except Exception:
                pass

            show_login()

            if had_session:
                notify(
                    "Account created. Please sign in to continue.",
                    GREEN,
                )
            else:
                notify(
                    "Account created. Please confirm your email, "
                    "then sign in.",
                    GREEN,
                )

        content = make_scrollable([
            app_logo(30),
            heading(
                "Create your account",
                "Join AROVI and meet people who share your intentions.",
            ),
            name,
            email,
            born,
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

                result = d.client.auth.sign_in_with_password({
                    "email": mail,
                    "password": secret,
                })

                if not result.user:
                    notify("Incorrect email address or password.")
                    return

                current_user_id = result.user.id

                profile = d.get_profile(current_user_id)

                if profile is None:

                    if state.get("admin_mode"):
                        d.client.auth.sign_out()
                        current_user_id = None
                        state["admin_mode"] = False
                        notify("This account is not authorized.")
                        return

                    # signed in, but has no AROVI profile yet
                    show_complete_profile()
                    return

                if is_blocked(profile):

                    d.client.auth.sign_out()
                    current_user_id = None

                    notify(
                        "This account has been suspended or banned. "
                        "Contact AROVI support if you think this is a "
                        "mistake."
                    )
                    return

                if state.get("admin_mode"):

                    if not profile.get("is_admin"):

                        d.client.auth.sign_out()
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
                text = friendly(ex)

                if "invalid login" in text.lower():
                    text = "Incorrect email address or password."

                notify(f"Sign-in failed: {text}")

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
                ft.Row(
                    alignment=ft.MainAxisAlignment.CENTER,
                    controls=[
                        ft.TextButton(
                            content="Forgot password?",
                            on_click=lambda e: show_forgot_password(),
                        )
                    ],
                ),
                secondary_button(
                    "Create a new account",
                    show_register,
                ),
            ],
        )

        show_screen(content)

    # ========================================================
    # FORGOT PASSWORD (6-digit code sent by email)
    # In Supabase > Authentication > Email Templates > "Reset Password",
    # add the line: Your code: {{ .Token }}
    # ========================================================

    def show_forgot_password(e=None):

        notice = make_notice()

        email = field(
            "Email address",
            keyboard=ft.KeyboardType.EMAIL,
        )

        def send(e):

            mail = (email.value or "").strip().lower()

            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", mail):
                notify("Enter a valid email address.")
                return

            auth = d.client.auth
            sender = (
                getattr(auth, "reset_password_for_email", None)
                or getattr(auth, "reset_password_email")
            )

            try:
                sender(mail)
            except Exception as ex:
                notify(f"Could not send the code: {friendly(ex)}")
                return

            show_reset_code(mail)

        show_screen(
            make_scrollable([
                app_logo(30),
                heading(
                    "Forgot password",
                    "Enter your email and we will send you a code.",
                ),
                email,
                notice,
                primary_button("SEND CODE", send),
                secondary_button("Back to sign in", show_login),
            ])
        )

    def show_reset_code(mail):

        notice = make_notice()

        code = field(
            "Code from the email",
            keyboard=ft.KeyboardType.NUMBER,
        )

        fresh = field("New password", password=True)
        confirm = field("Confirm new password", password=True)

        def save(e):

            token = (code.value or "").strip()
            new = fresh.value or ""

            if not token or not new:
                notify("Enter the code and a new password.")
                return

            if len(new) < 8:
                notify("New password must be at least 8 characters.")
                return

            if new != (confirm.value or ""):
                notify("The new passwords do not match.")
                return

            try:
                d.client.auth.verify_otp({
                    "email": mail,
                    "token": token,
                    "type": "recovery",
                })
                d.client.auth.update_user({"password": new})
            except Exception as ex:
                try:
                    d.client.auth.sign_out()
                except Exception:
                    pass

                notify(
                    f"Could not reset the password: {friendly(ex)} "
                    "Check the code and try again."
                )
                return

            try:
                d.client.auth.sign_out()
            except Exception:
                pass

            show_login()
            notify("Password updated. Please sign in.", GREEN)

        show_screen(
            make_scrollable([
                app_logo(30),
                heading(
                    "Enter your code",
                    f"We sent a code to {mail}.",
                ),
                code,
                fresh,
                confirm,
                notice,
                primary_button("RESET PASSWORD", save),
                secondary_button("Back to sign in", show_login),
            ])
        )

    # ========================================================
    # FINISH PROFILE (signed in but no AROVI profile, e.g. an
    # account created in another app on the same Supabase project)
    # ========================================================

    def show_complete_profile():

        notice = make_notice()

        name = field("Full name")
        born = field("Date of birth (YYYY-MM-DD)")
        gender = gender_dropdown()
        interested = interest_dropdown()
        location = field("City / Location")

        terms = ft.Checkbox(
            label=(
                "I confirm that I am 18 or older and agree to the "
                "Terms and Privacy Policy."
            )
        )

        def save(e):

            full_name = (name.value or "").strip()
            place = (location.value or "").strip()

            if not full_name or not place:
                notify("Please complete all required fields.")
                return

            birth, problem = read_birth_date(born.value)

            if problem:
                notify(problem)
                return

            if not gender.value or not interested.value:
                notify("Select your gender and preference.")
                return

            if not terms.value:
                notify("Please confirm the 18+ requirement and the terms.")
                return

            try:
                d.create_profile(
                    full_name,
                    birth,
                    gender.value,
                    interested.value,
                    place,
                )
            except Exception as ex:
                notify(friendly(ex))
                return

            open_dashboard()

        show_screen(
            make_scrollable([
                app_logo(30),
                heading(
                    "Finish setting up",
                    "Your login works, but it has no AROVI profile yet.",
                ),
                name,
                born,
                gender,
                interested,
                location,
                terms,
                notice,
                primary_button("CONTINUE", save),
                secondary_button("SIGN OUT", lambda e: sign_out_all()),
            ])
        )

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

        # a different user signed in -> start with fresh data
        if state["user"] != current_user_id:
            state.update(
                queue=None,
                index=0,
                user=current_user_id,
                matches=None,
            )

        try:
            user = d.get_profile(current_user_id)

        except Exception as ex:

            show_screen(
                error_view(
                    "Connection problem",
                    friendly(ex),
                    open_dashboard,
                )
            )
            return

        if user is None:
            show_complete_profile()
            return

        if is_blocked(user):

            sign_out_all()
            notify(
                "This account has been suspended or banned. "
                "Contact AROVI support if you think this is a mistake."
            )
            return

        refresh_unread()

        if current_tab == "Matches":
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
                d.report_user(person_id, text)
            except Exception as ex:
                notify(friendly(ex))
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
                        lambda e: open_chat(person, "Matches"),
                    ),
                    secondary_button(
                        "KEEP BROWSING",
                        lambda e: open_dashboard(),
                    ),
                ],
            )
        )

    def announcement_banner():
        note = state.get("announcement")

        if not note or note.get("id") == state.get("dismissed"):
            return None

        def dismiss(e):
            state["dismissed"] = note.get("id")
            show_discover()

        return panel(
            ft.Column(
                spacing=4,
                controls=[
                    ft.Text(
                        note.get("title") or "",
                        size=14,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    ),
                    ft.Text(
                        note.get("body") or "",
                        size=13,
                        color=DARK,
                    ),
                    ft.TextButton(content="Dismiss", on_click=dismiss),
                ],
            )
        )

    def show_discover():

        try:
            user = d.get_profile(current_user_id)

            if user is None:
                open_dashboard()
                return

            if state["queue"] is None:
                state["queue"] = d.get_discover()
                state["index"] = 0

                try:
                    state["announcement"] = d.get_announcement()
                except Exception:
                    state["announcement"] = None

        except Exception as ex:

            show_screen(
                error_view(
                    "Could not load Discover",
                    friendly(ex),
                    reload_discover,
                ),
                navigation=True,
            )
            return

        notice = make_notice()
        banner = announcement_banner()

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

        top = [header] + ([banner] if banner else [])

        # Explain WHY Discover is empty instead of a blank screen
        if not user.get("gender") or not user.get("interested_in"):

            show_screen(
                ft.Column(
                    expand=True,
                    spacing=14,
                    controls=top + [
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
                    controls=top + [
                        empty_view(
                            ft.Icons.EXPLORE_OUTLINED,
                            "No new profiles right now",
                            "You've seen everyone who fits your "
                            "preferences. Widen your age range on the "
                            "Profile tab or check back later.",
                        ),
                        wide_button("REFRESH", reload_discover),
                        outline_wide(
                            "ADJUST PREFERENCES",
                            lambda e: set_tab("Profile"),
                        ),
                    ],
                ),
                navigation=True,
            )
            return

        person = queue[idx]

        try:
            person_photos = d.get_photos(person["id"])
        except Exception:
            person_photos = []

        def like_person(e):

            try:
                mutual = d.like_person(person["id"])
            except Exception as ex:
                notify(f"Could not send like: {friendly(ex)}")
                return

            state["index"] += 1

            if mutual:
                state["matches"] = None
                show_match_screen(person)
            else:
                show_discover()
                notify(
                    "Like sent. If they like you back, you'll match.",
                    GREEN,
                )

        def skip_person(e):

            try:
                d.pass_person(person["id"])
            except Exception:
                pass

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
            make_scrollable(
                top + [
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
                ]
            ),
            navigation=True,
        )

    # ========================================================
    # MATCHES (matched members are locked in: they leave Discover
    # and can only be reached through Matches / Messages)
    # ========================================================

    def show_like_profile(person):
        """A member who liked you: like back (match) or pass."""

        pid = person["id"]
        name = display_name(person)

        try:
            photos = d.get_photos(pid)
        except Exception:
            photos = []

        notice = make_notice()

        def here():
            show_like_profile(person)

        def like_back(e):
            try:
                mutual = d.like_person(pid)
            except Exception as ex:
                notify(friendly(ex))
                return

            state["queue"] = None
            state["matches"] = None

            if mutual:
                show_match_screen(person)
            else:
                show_matches()

        def decline(e):
            try:
                d.pass_person(pid)
            except Exception as ex:
                notify(friendly(ex))
                return

            state["queue"] = None
            show_matches()
            notify(f"You passed on {name}.", MUTED)

        bio_text = (person.get("bio") or "").strip()

        show_screen(
            make_scrollable([
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.ARROW_BACK,
                            icon_color=PRIMARY,
                            on_click=lambda e: show_matches(),
                        ),
                        ft.Text(
                            f"{name} liked you",
                            size=18,
                            weight=ft.FontWeight.BOLD,
                            color=DARK,
                        ),
                    ]
                ),
                panel(
                    ft.Column(
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12,
                        controls=[
                            gallery_view(photos, person),
                            ft.Text(
                                f"{name}, {person.get('age', '')}",
                                size=24,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            ft.Text(
                                person.get("location") or "",
                                size=13,
                                color=MUTED,
                            ),
                            ft.Text(
                                bio_text or "No bio yet.",
                                size=14,
                                color=DARK if bio_text else MUTED,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                    ),
                    padding=24,
                ),
                notice,
                wide_button("LIKE BACK", like_back),
                outline_wide(
                    "PASS",
                    decline,
                    icon=ft.Icons.CLOSE,
                ),
                outline_wide(
                    "REPORT",
                    lambda e: show_report(pid, name, here),
                    icon=ft.Icons.FLAG_OUTLINED,
                ),
            ]),
            navigation=True,
        )

    def load_matches():
        matches = d.get_matches()
        state["matches"] = matches
        return matches

    def show_matches():

        try:
            matches = load_matches()

            try:
                likes = d.get_likes_received()
            except Exception:
                likes = []
        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load matches",
                    friendly(ex),
                    open_dashboard,
                ),
                navigation=True,
            )
            return

        notice = make_notice()

        fresh = [m for m in matches if not m.get("last_message")]
        talking = [m for m in matches if m.get("last_message")]

        controls = [
            app_logo(25),
            heading(
                "Your matches",
                f"{len(matches)} mutual connection(s). Matched members "
                "no longer appear in Discover.",
            ),
            notice,
        ]

        if likes:

            controls.append(
                ft.Text(
                    f"Likes you ({len(likes)})",
                    size=16,
                    weight=ft.FontWeight.BOLD,
                    color=DARK,
                )
            )

            for person in likes:
                controls.append(
                    person_row(
                        person,
                        person_subtitle(person),
                        lambda e, p=person: show_like_profile(p),
                        trailing=ft.Text(
                            "Like back?",
                            size=11,
                            weight=ft.FontWeight.BOLD,
                            color=PRIMARY,
                        ),
                    )
                )

            if matches:
                controls.append(
                    ft.Text(
                        "Matches",
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=DARK,
                    )
                )

        if not matches and not likes:

            controls.append(
                empty_view(
                    ft.Icons.FAVORITE_BORDER,
                    "No matches yet",
                    "Like profiles in Discover. When someone likes you "
                    "back, the match will appear here.",
                )
            )

        for person in fresh + talking:

            is_new = not person.get("last_message")
            subtitle = person_subtitle(person)

            if is_new:
                subtitle = "New match \u2022 " + subtitle if subtitle else "New match"

            controls.append(
                person_row(
                    person,
                    subtitle,
                    lambda e, p=person: show_member_profile(p, show_matches),
                    trailing=ft.Text(
                        "Matched",
                        size=11,
                        weight=ft.FontWeight.BOLD,
                        color=GREEN,
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
            matches = load_matches()
            refresh_unread()
        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load messages",
                    friendly(ex),
                    open_dashboard,
                ),
                navigation=True,
            )
            return

        controls = [
            app_logo(25),
            heading(
                "Messages",
                "Continue conversations with your matches.",
            ),
        ]

        if not matches:

            controls.append(
                empty_view(
                    ft.Icons.CHAT_BUBBLE_OUTLINE,
                    "No conversations yet",
                    "You need a mutual match before you can "
                    "start messaging.",
                )
            )

        for person in matches:

            last = person.get("last_message")

            if last:
                mine = (
                    str(person.get("last_sender"))
                    == str(current_user_id)
                )
                preview = ("You: " if mine else "") + last
            else:
                preview = "Say hello \U0001f44b"

            unread = int(person.get("unread") or 0)

            controls.append(
                person_row(
                    person,
                    preview,
                    lambda e, p=person: open_chat(p, "Messages"),
                    trailing=unread_pill(unread) if unread else None,
                )
            )

        show_screen(
            make_scrollable(controls),
            navigation=True,
        )

    # ========================================================
    # PRIVATE CHAT
    # ========================================================

    def open_chat(person, origin_tab="Messages"):

        other_id = person["id"]

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

        def mark_seen(messages):
            unseen = [
                m for m in messages
                if str(m.get("sender_id")) == str(other_id)
                and not m.get("read_at")
            ]

            if unseen:
                try:
                    d.mark_read(other_id)
                    refresh_unread()
                except Exception:
                    pass

        def send_message(e=None):

            message = (message_input.value or "").strip()

            if not message:
                return

            if len(message) > 2000:
                notify("Messages cannot exceed 2,000 characters.")
                return

            try:
                d.send_message(other_id, message)
                message_input.value = ""

                render(
                    d.get_conversation(current_user_id, other_id)
                )

            except Exception as ex:
                notify(friendly(ex))

        message_input.on_submit = send_message

        try:
            history = d.get_conversation(current_user_id, other_id)
            render(history, update=False)
            mark_seen(history)
        except Exception as ex:
            chat_list.controls = [
                ft.Text(
                    f"Could not load conversation: {friendly(ex)}",
                    color=PRIMARY,
                )
            ]

        def back_to_chat():
            open_chat(person, origin_tab)

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
                                ft.Container(
                                    content=person_avatar(42, person),
                                    on_click=lambda e: show_member_profile(
                                        person,
                                        back_to_chat,
                                    ),
                                ),
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
                                    icon=ft.Icons.INFO_OUTLINE,
                                    icon_color=MUTED,
                                    tooltip="Profile, unmatch or block",
                                    on_click=lambda e: show_member_profile(
                                        person,
                                        back_to_chat,
                                    ),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.FLAG_OUTLINED,
                                    icon_color=MUTED,
                                    tooltip="Report",
                                    on_click=lambda e: show_report(
                                        other_id,
                                        display_name(person),
                                        back_to_chat,
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
                        d.get_conversation,
                        current_user_id,
                        other_id,
                    )

                    if (
                        state["chat_token"] == token
                        and len(rows) != shown["count"]
                    ):
                        render(rows)
                        await asyncio.to_thread(mark_seen, rows)

                except Exception:
                    pass

        page.run_task(poll)

    # ========================================================
    # MEMBER PROFILE (a match): message, unmatch, block, report
    # ========================================================

    def show_member_profile(person, back):

        pid = person["id"]
        name = display_name(person)

        try:
            photos = d.get_photos(pid)
        except Exception:
            photos = []

        notice = make_notice()

        def here():
            show_member_profile(person, back)

        def do_unmatch():
            try:
                d.unmatch_person(pid)
            except Exception as ex:
                notify(friendly(ex))
                return

            state["queue"] = None
            state["matches"] = None
            set_tab("Matches")
            notify(f"You unmatched with {name}.", GREEN)

        def do_block():
            try:
                d.block_person(pid)
            except Exception as ex:
                notify(friendly(ex))
                return

            state["queue"] = None
            state["matches"] = None
            set_tab("Matches")
            notify(f"{name} is blocked. You can undo this in Profile.", GREEN)

        bio_text = (person.get("bio") or "").strip()

        show_screen(
            make_scrollable([
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.ARROW_BACK,
                            icon_color=PRIMARY,
                            on_click=lambda e: back(),
                        ),
                        ft.Text(
                            name,
                            size=18,
                            weight=ft.FontWeight.BOLD,
                            color=DARK,
                        ),
                    ]
                ),
                panel(
                    ft.Column(
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12,
                        controls=[
                            gallery_view(photos, person),
                            ft.Text(
                                f"{name}, {person.get('age', '')}",
                                size=24,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            ft.Text(
                                person.get("location") or "",
                                size=13,
                                color=MUTED,
                            ),
                            ft.Text(
                                bio_text or "No bio yet.",
                                size=14,
                                color=DARK if bio_text else MUTED,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                    ),
                    padding=24,
                ),
                notice,
                wide_button(
                    "SEND A MESSAGE",
                    lambda e: open_chat(person, "Matches"),
                ),
                outline_wide(
                    "UNMATCH",
                    lambda e: confirm_view(
                        f"Unmatch with {name}?",
                        "Your conversation is deleted for both of you and "
                        "you won't see each other again.",
                        "UNMATCH",
                        do_unmatch,
                        here,
                    ),
                    icon=ft.Icons.HEART_BROKEN_OUTLINED,
                ),
                outline_wide(
                    "BLOCK",
                    lambda e: confirm_view(
                        f"Block {name}?",
                        "You will be unmatched and neither of you can see "
                        "the other or send messages.",
                        "BLOCK",
                        do_block,
                        here,
                    ),
                    icon=ft.Icons.BLOCK,
                ),
                outline_wide(
                    "REPORT",
                    lambda e: show_report(pid, name, here),
                    icon=ft.Icons.FLAG_OUTLINED,
                ),
            ]),
            navigation=True,
        )

    # ========================================================
    # BLOCKED MEMBERS
    # ========================================================

    def show_blocked(back=None):

        back = back or show_profile

        try:
            blocked = d.get_blocked()
        except Exception as ex:
            show_screen(
                error_view(
                    "Could not load blocked members",
                    friendly(ex),
                    show_blocked,
                ),
                navigation=True,
            )
            return

        notice = make_notice()

        def unblock(person):
            try:
                d.unblock_person(person["id"])
            except Exception as ex:
                notify(friendly(ex))
                return

            state["queue"] = None
            show_blocked(back)
            notify(f"{display_name(person)} was unblocked.", GREEN)

        controls = [
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        icon_color=PRIMARY,
                        on_click=lambda e: back(),
                    ),
                    heading("Blocked members"),
                ]
            ),
            notice,
        ]

        if not blocked:
            controls.append(
                empty_view(
                    ft.Icons.BLOCK,
                    "No blocked members",
                    "People you block will be listed here.",
                )
            )

        for person in blocked:
            controls.append(
                person_row(
                    person,
                    person_subtitle(person),
                    None,
                    trailing=ft.TextButton(
                        content="Unblock",
                        on_click=lambda e, p=person: unblock(p),
                    ),
                )
            )

        show_screen(make_scrollable(controls), navigation=True)

    # ========================================================
    # PROFILE
    # ========================================================

    def show_profile():

        try:
            user = d.get_profile(current_user_id)

        except Exception as ex:

            show_screen(
                error_view(
                    "Could not load your profile",
                    friendly(ex),
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

        gender = gender_dropdown("Gender", user.get("gender"))
        interested = interest_dropdown("Interested in", user.get("interested_in"))

        min_age = field("Preferred minimum age", keyboard=ft.KeyboardType.NUMBER)
        min_age.value = str(user.get("min_age") or 18)

        max_age = field("Preferred maximum age", keyboard=ft.KeyboardType.NUMBER)
        max_age.value = str(user.get("max_age") or 99)

        visible = ft.Switch(
            label="Show my profile in Discover",
            value=user.get("is_active") is not False,
            active_color=PRIMARY,
        )

        def save_profile(e):

            updated_name = (name.value or "").strip()

            if not updated_name:
                notify("Your name cannot be empty.")
                return

            if not gender.value or not interested.value:
                notify("Select your gender and preference.")
                return

            try:
                low = int(min_age.value or "18")
                high = int(max_age.value or "99")
            except ValueError:
                notify("Enter whole numbers for the age range.")
                return

            if low < 18 or high > 99 or low > high:
                notify("Age range must be between 18 and 99 (minimum first).")
                return

            try:

                d.update_profile(current_user_id, {
                    "name": updated_name,
                    "location": (location.value or "").strip(),
                    "bio": (bio.value or "").strip()[:500],
                    "gender": gender.value,
                    "interested_in": interested.value,
                    "min_age": low,
                    "max_age": high,
                    "is_active": bool(visible.value),
                })

                # preferences may have changed -> rebuild Discover
                state["queue"] = None

                notify(
                    "Profile updated successfully.",
                    GREEN,
                )

            except Exception as ex:
                notify(f"Profile update error: {friendly(ex)}")

        def delete_account():
            try:
                d.delete_account(current_user_id)
            except Exception as ex:
                notify(friendly(ex))
                return

            sign_out_all()
            notify("Your AROVI account has been deleted.", GREEN)

        # ---------- photos ----------
        try:
            photos = d.get_photos(current_user_id)
        except Exception as ex:
            photos = []
            notice.value = f"Could not load photos: {friendly(ex)}"

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
                        d.upload_photo,
                        current_user_id,
                        f.name,
                        data,
                    )
                    added += 1

                except Exception as ex:
                    last_error = f"{f.name}: {friendly(ex)}"

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
                    d.set_main_photo(current_user_id, photo["path"])
                except Exception as ex:
                    notify(f"Could not set main photo: {friendly(ex)}")
                    return
                show_profile()

            def remove(e):
                try:
                    d.delete_photo(current_user_id, photo)
                except Exception as ex:
                    notify(f"Could not delete photo: {friendly(ex)}")
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
                        ft.Text(
                            f"Age {user.get('age')} (from your date of birth)",
                            color=MUTED,
                            size=12,
                        ),
                    ],
                )
            ),
            photo_section,
            name,
            gender,
            interested,
            location,
            bio,
            min_age,
            max_age,
            visible,
            notice,
            wide_button(
                "SAVE PROFILE",
                save_profile,
            ),
            outline_wide(
                "BLOCKED MEMBERS",
                lambda e: show_blocked(show_profile),
                icon=ft.Icons.BLOCK,
            ),
            outline_wide(
                "SAFETY TIPS",
                lambda e: show_safety(show_profile),
                icon=ft.Icons.SHIELD_OUTLINED,
            ),
            ft.Row(
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=0,
                controls=[
                    ft.TextButton(
                        content="Terms",
                        on_click=lambda e: show_terms(show_profile),
                    ),
                    ft.TextButton(
                        content="Privacy",
                        on_click=lambda e: show_privacy(show_profile),
                    ),
                ],
            ),
            outline_wide(
                "CHANGE PASSWORD",
                lambda e: show_change_password(show_profile),
                icon=ft.Icons.LOCK_OUTLINE,
            ),
            outline_wide(
                "LOG OUT",
                lambda e: sign_out_all(),
                icon=ft.Icons.LOGOUT,
            ),
            outline_wide(
                "DELETE MY ACCOUNT",
                lambda e: confirm_view(
                    "Delete your AROVI account?",
                    "This permanently deletes your profile, photos, "
                    "matches and messages. This cannot be undone.",
                    "DELETE MY ACCOUNT",
                    delete_account,
                    show_profile,
                ),
                icon=ft.Icons.DELETE_FOREVER,
            ),
            ft.Text(
                f"AROVI {APP_VERSION} \u2022 18+ COMMUNITY",
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
    # SAFETY, TERMS AND PRIVACY (plain information pages)
    # Have a lawyer review the Terms and Privacy text before launch.
    # ========================================================

    def show_text_page(title, intro, sections, back):

        controls = [
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        icon_color=PRIMARY,
                        on_click=lambda e: back(),
                    ),
                    heading(title),
                ]
            ),
            ft.Text(intro, size=13, color=MUTED),
        ]

        for head, text in sections:
            controls.append(
                panel(
                    ft.Column(
                        spacing=4,
                        controls=[
                            ft.Text(
                                head,
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            ft.Text(text, size=13, color=DARK),
                        ],
                    )
                )
            )

        show_screen(
            make_scrollable(controls),
            navigation=bool(current_user_id and not state.get("admin")),
        )

    def show_safety(back):
        show_text_page(
            "Safety tips",
            "Meeting new people is exciting. Stay safe while you do it.",
            [
                (
                    "Protect your information",
                    "Do not share your home address, workplace, financial "
                    "details or passwords with someone you have just met.",
                ),
                (
                    "Never send money",
                    "Anyone who asks for money, gift cards or crypto is "
                    "probably a scammer, however convincing the story. "
                    "Report them.",
                ),
                (
                    "Take your time",
                    "Chat in AROVI first. Video call before meeting if you "
                    "can, and be wary of anyone who rushes you or moves "
                    "the chat elsewhere straight away.",
                ),
                (
                    "Meet safely",
                    "Meet in a busy public place, tell a friend where you "
                    "are going, arrange your own transport and keep your "
                    "phone charged.",
                ),
                (
                    "Trust your instincts",
                    "If something feels wrong, leave. You can unmatch, "
                    "block and report anyone from their profile or chat.",
                ),
            ],
            back,
        )

    def show_terms(back):
        show_text_page(
            "Terms of use",
            "By using AROVI you agree to these terms.",
            [
                (
                    "Who can use AROVI",
                    "AROVI is for adults aged 18 or over. You must give "
                    "accurate information, keep one account only and keep "
                    "your password private.",
                ),
                (
                    "Behave respectfully",
                    "No harassment, hate, threats, scams, spam, "
                    "impersonation, nudity or sexual content, or anything "
                    "illegal. Do not use AROVI for commercial promotion.",
                ),
                (
                    "Moderation",
                    "We may review reports and conversations that are "
                    "reported, remove content and suspend or ban accounts "
                    "that break these rules, without notice where needed.",
                ),
                (
                    "Your content",
                    "You keep ownership of your photos and text, and give "
                    "AROVI permission to show them to other members in the "
                    "app.",
                ),
                (
                    "Ending your account",
                    "You can delete your account at any time in Profile. "
                    "We may close accounts that break these terms.",
                ),
                (
                    "No guarantees",
                    "AROVI does not run background checks and cannot "
                    "promise you will meet anyone. The service is provided "
                    "as is. Take care when meeting people.",
                ),
                (
                    "Contact",
                    f"Questions or concerns: {SUPPORT_EMAIL}",
                ),
            ],
            back,
        )

    def show_privacy(back):
        show_text_page(
            "Privacy policy",
            "How AROVI handles your information.",
            [
                (
                    "What we collect",
                    "Your email, name, date of birth, gender, who you are "
                    "interested in, location text, bio, photos, likes, "
                    "matches, messages and any reports you send.",
                ),
                (
                    "Who sees what",
                    "Other members see your name, age, gender, location, "
                    "bio and photos. They never see your email or date of "
                    "birth. Only matches can message you.",
                ),
                (
                    "Moderators",
                    "AROVI administrators can view account details and, "
                    "when a conversation is reported, that conversation, "
                    "to keep members safe.",
                ),
                (
                    "Storage and security",
                    "Data is stored with our hosting provider (Supabase) "
                    "and sent over encrypted connections. Photos are kept "
                    "in a private store and shown through temporary links.",
                ),
                (
                    "Your choices",
                    "Edit your profile, hide it from Discover, block or "
                    "report people, or delete your account from Profile. "
                    "Deleting removes your profile, photos, matches and "
                    "messages.",
                ),
                (
                    "Adults only",
                    "AROVI is not for anyone under 18. If you believe a "
                    "minor is using AROVI, please report the profile.",
                ),
                (
                    "Contact",
                    f"Privacy questions or requests: {SUPPORT_EMAIL}",
                ),
            ],
            back,
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
                profile = d.get_profile(current_user_id) or {}
                mail = profile.get("email") or d.auth_email()

                d.client.auth.sign_in_with_password({
                    "email": mail,
                    "password": old,
                })
            except Exception:
                notify("Your current password is incorrect.")
                return

            try:
                d.client.auth.update_user({"password": new})
            except Exception as ex:
                notify(f"Could not change password: {friendly(ex)}")
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
                ("More", ft.Icons.MORE_HORIZ),
            ],
            state.get("atab", "Overview"),
            set_atab,
        )

    def admin_screen(content):
        show_screen(content, nav=admin_nav())

    def admin_error(title, ex, retry):
        show_screen(error_view(title, ex, retry), nav=admin_nav())

    def admin_logout(e=None):
        sign_out_all()

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
            allowed = d.check_admin(current_user_id)
        except Exception as ex:
            show_screen(
                error_view("Connection problem", ex, open_admin)
            )
            return

        if not allowed:

            try:
                d.client.auth.sign_out()
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
        elif tab == "More":
            admin_more()
        else:
            admin_overview()

    # ---------- overview ----------

    def admin_overview():

        try:
            stats = d.admin_stats()
            recent = d.admin_list_users("", "all", 0, 5)
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
            rows = d.admin_list_users(
                query,
                current_filter,
                page_number * ADMIN_PAGE,
                ADMIN_PAGE + 1,
            )
            stats = d.admin_stats()
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
            person = d.get_profile(user_id)

            if not person:
                back()
                return

            summary = d.admin_user_summary(user_id)
            matches = d.admin_user_matches(user_id)

            try:
                photos = d.get_photos(user_id)
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
                lambda: d.admin_remove_photo(photo),
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
                                    lambda: d.admin_unmatch(mid),
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
                            lambda: d.admin_set_admin(user_id, False),
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
                            lambda: d.admin_set_status(user_id, "suspended"),
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
                                lambda: d.admin_set_status(user_id, "banned"),
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
                            lambda: d.admin_set_status(user_id, "active"),
                            "User reactivated.",
                            refresh,
                        ),
                    )
                )

            actions.append(
                outline_wide(
                    "HIDE FROM DISCOVER" if visible_now else "SHOW IN DISCOVER",
                    lambda e: admin_run(
                        lambda: d.admin_set_visibility(user_id, not visible_now),
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
                                lambda: d.admin_clear_bio(user_id),
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
                            lambda: d.admin_set_admin(user_id, True),
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
                        "This permanently deletes the member's AROVI "
                        "profile, photos, likes, matches and messages. "
                        "Their login is kept, so Ban instead if you want "
                        "to stop them rejoining. It cannot be undone.",
                        "DELETE FOREVER",
                        lambda: admin_run(
                            lambda: d.admin_delete_user(user_id),
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
            rows = d.admin_recent_photos(page_number * size, size + 1)
            has_next = len(rows) > size
            rows = rows[:size]
            urls = d.signed_urls([r["path"] for r in rows]) if rows else {}
        except Exception as ex:
            admin_error("Could not load photos", ex, admin_photos)
            return

        def remove(photo):
            admin_run(
                lambda: d.admin_remove_photo(photo),
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
            messages = d.admin_get_conversation(first_id, second_id)
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
            reports = d.admin_list_reports(mode)
        except Exception as ex:
            admin_error("Could not load reports", ex, admin_reports)
            return

        names = {}

        for r in reports:
            names[str(r["reporter_id"])] = r.get("reporter_name") or "Member"
            names[str(r["reported_id"])] = r.get("reported_name") or "Member"

        def dismiss(report):
            admin_run(
                lambda: d.admin_resolve_report(report["id"], "dismissed"),
                "Report dismissed.",
                admin_reports,
            )

        def sanction(report, status):
            def action():
                d.admin_set_status(report["reported_id"], status)
                d.admin_resolve_report(report["id"], "resolved")

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

    # ---------- more tools: announcements, activity log ----------

    def admin_more():

        admin_screen(
            make_scrollable([
                admin_header("More", "Tools for running AROVI."),
                make_notice(),
                outline_wide(
                    "ANNOUNCEMENTS",
                    lambda e: admin_announcements(),
                    icon=ft.Icons.CAMPAIGN_OUTLINED,
                ),
                outline_wide(
                    "ACTIVITY LOG",
                    lambda e: admin_log(),
                    icon=ft.Icons.HISTORY,
                ),
                outline_wide(
                    "CHANGE MY PASSWORD",
                    lambda e: show_change_password(open_admin),
                    icon=ft.Icons.LOCK_RESET,
                ),
                outline_wide(
                    "SIGN OUT",
                    admin_logout,
                    icon=ft.Icons.LOGOUT,
                ),
            ])
        )

    def admin_announcements():

        try:
            items = d.admin_list_announcements()
        except Exception as ex:
            admin_error("Could not load announcements", ex, admin_announcements)
            return

        title = field("Title")
        body = ft.TextField(
            label="Message to all members",
            multiline=True,
            min_lines=3,
            max_lines=5,
            filled=True,
            bgcolor=WHITE,
            border_radius=12,
        )

        def post(e):

            heading_text = (title.value or "").strip()
            body_text = (body.value or "").strip()

            if not heading_text or not body_text:
                notify("Enter a title and a message.")
                return

            if len(heading_text) > 80 or len(body_text) > 400:
                notify("Keep the title under 80 and the message under 400 characters.")
                return

            admin_run(
                lambda: d.admin_post_announcement(heading_text, body_text),
                "Announcement posted. Members see it on Discover.",
                admin_announcements,
            )

        controls = [
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        icon_color=PRIMARY,
                        on_click=lambda e: admin_more(),
                    ),
                    heading("Announcements"),
                ]
            ),
            ft.Text(
                "The newest active announcement shows at the top of "
                "every member's Discover screen.",
                size=12,
                color=MUTED,
            ),
            title,
            body,
            make_notice(),
            wide_button("POST ANNOUNCEMENT", post),
        ]

        for item in items:

            is_on = bool(item.get("active"))

            controls.append(
                panel(
                    ft.Column(
                        spacing=4,
                        controls=[
                            ft.Text(
                                item.get("title") or "",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            ft.Text(
                                item.get("body") or "",
                                size=13,
                                color=DARK,
                            ),
                            ft.Text(
                                ("Live" if is_on else "Hidden")
                                + " \u2022 "
                                + str(item.get("created_at") or "")[:10],
                                size=11,
                                color=GREEN if is_on else MUTED,
                            ),
                            ft.Row(
                                spacing=0,
                                controls=[
                                    ft.TextButton(
                                        content="Hide" if is_on else "Show",
                                        on_click=lambda e, i=item, on=is_on: admin_run(
                                            lambda: d.admin_set_announcement_active(
                                                i["id"],
                                                not on,
                                            ),
                                            "Announcement updated.",
                                            admin_announcements,
                                        ),
                                    ),
                                    ft.TextButton(
                                        content="Delete",
                                        on_click=lambda e, i=item: admin_confirm(
                                            "Delete this announcement?",
                                            "It disappears for everyone.",
                                            "DELETE",
                                            lambda: admin_run(
                                                lambda: d.admin_delete_announcement(
                                                    i["id"]
                                                ),
                                                "Announcement deleted.",
                                                admin_announcements,
                                            ),
                                            admin_announcements,
                                        ),
                                    ),
                                ],
                            ),
                        ],
                    )
                )
            )

        admin_screen(make_scrollable(controls))

    def admin_log(page=None):

        if page is not None:
            state["alog_page"] = max(0, page)

        page_number = state.get("alog_page", 0)
        size = 25

        try:
            rows = d.admin_list_log(page_number * size, size + 1)
        except Exception as ex:
            admin_error("Could not load the activity log", ex, admin_log)
            return

        has_next = len(rows) > size
        rows = rows[:size]

        controls = [
            ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        icon_color=PRIMARY,
                        on_click=lambda e: admin_more(),
                    ),
                    heading("Activity log", "What admins have done."),
                ]
            ),
        ]

        if not rows:
            controls.append(
                empty_view(
                    ft.Icons.HISTORY,
                    "Nothing yet",
                    "Admin actions will be recorded here.",
                )
            )

        for row in rows:

            who = row.get("admin_name") or "Admin"
            target = row.get("target_name")
            detail = row.get("detail") or ""
            stamp = str(row.get("created_at") or "")[:16].replace("T", " ")

            line = f"{who} \u2022 {row.get('action') or ''}"

            if target:
                line += f" \u2022 {target}"

            controls.append(
                panel(
                    ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text(line, size=13, color=DARK),
                            ft.Text(
                                (detail + " " if detail else "") + stamp,
                                size=11,
                                color=MUTED,
                            ),
                        ],
                    ),
                    padding=12,
                )
            )

        if page_number > 0 or has_next:
            controls.append(
                pager(
                    page_number,
                    has_next,
                    lambda n: admin_log(page=n),
                )
            )

        admin_screen(make_scrollable(controls))

    # ========================================================
    # START APPLICATION
    # ========================================================

    if boot_error or d is None:

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
                        boot_error or "Supabase is not configured.",
                        color=PRIMARY,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "Add SUPABASE_URL and SUPABASE_KEY (and "
                        "optionally AROVI_SCHEMA) to the environment "
                        "variables or a .env file beside main.py, "
                        "then restart AROVI.",
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
