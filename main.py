
import os
import re
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
        raise RuntimeError(supabase_error or "Supabase is unavailable.")
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

    return result.data[0] if result.data else None


def get_auth_user():
    if supabase is None:
        return None

    try:
        result = supabase.auth.get_user()
        return result.user if result else None
    except Exception:
        return None


def gender_label(gender):
    return {
        "Man": "Men",
        "Woman": "Women",
        "Non-binary": "Non-binary",
    }.get(gender, "")


def preferences_match(first, second):
    first_gender = gender_label(first.get("gender", ""))
    second_gender = gender_label(second.get("gender", ""))

    first_pref = first.get("interested_in", "")
    second_pref = second.get("interested_in", "")

    first_accepts = (
        first_pref == "Everyone" or first_pref == first_gender
    )

    second_accepts = (
        second_pref == "Everyone" or second_pref == second_gender
    )

    return first_accepts and second_accepts


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
        .eq("is_active", True)
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

        if (
            person_id
            and person_id not in excluded
            and age >= 18
            and preferences_match(me, person)
        ):
            results.append(person)

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
    sender_id = str(sender_id)
    receiver_id = str(receiver_id)

    if sender_id == receiver_id:
        return False

    api_client = api()

    receiver = get_profile(receiver_id)

    if not receiver or not receiver.get("is_active"):
        raise ValueError("This profile is unavailable.")

    api_client.table("likes").upsert(
        {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
        },
        on_conflict="sender_id,receiver_id",
    ).execute()

    reverse = (
        api_client.table("likes")
        .select("id")
        .eq("sender_id", receiver_id)
        .eq("receiver_id", sender_id)
        .limit(1)
        .execute()
        .data or []
    )

    if reverse:
        one, two = sorted([sender_id, receiver_id])

        api_client.table("matches").upsert(
            {
                "user_one": one,
                "user_two": two,
            },
            on_conflict="user_one,user_two",
        ).execute()

        return True

    return False


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
        .select("message")
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
# FLET APPLICATION
# ============================================================

def main(page: ft.Page):

    page.title = "AROVI - Meaningful Connections"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = BG
    page.padding = 0
    page.spacing = 0

    auth_user = get_auth_user()
    current_user_id = auth_user.id if auth_user else None

    current_tab = "Discover"
    active_notice = None

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

    def person_avatar(size=54):
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

    def make_scrollable(controls):
        return ft.Column(
            controls=controls,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=14,
        )

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
        f"Registration error: {str(ex)}"
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
                        "This account does not meet the 18+ requirement."
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

        if not current_user_id:
            show_welcome()
            return

        try:
            user = get_profile(current_user_id)

        except Exception as ex:

            show_screen(
                ft.Column(
                    controls=[
                        app_logo(25),
                        heading("Connection problem"),
                        ft.Text(
                            f"Could not load your account: {ex}",
                            color=PRIMARY,
                        ),
                        secondary_button(
                            "Retry",
                            lambda e: open_dashboard(),
                        ),
                    ]
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

    def show_discover():

        try:

            user = get_profile(current_user_id)
            profiles = get_unseen_profiles(current_user_id)

        except Exception as ex:

            show_screen(
                ft.Column(
                    controls=[
                        app_logo(25),
                        heading(
                            "Discover",
                            "Could not load profiles.",
                        ),
                        ft.Text(str(ex), color=PRIMARY),
                        secondary_button(
                            "Retry",
                            lambda e: show_discover(),
                        ),
                    ]
                ),
                navigation=True,
            )
            return

        notice = make_notice()

        first_name = (
            (user.get("name") or "there").split()[0]
        )

        controls = [
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    app_logo(25),
                    ft.Text(
                        f"Hello, {first_name}",
                        color=DARK,
                        weight=ft.FontWeight.BOLD,
                    ),
                ],
            ),
            heading(
                "Discover",
                "Explore adults who may share your interests.",
            ),
            notice,
        ]

        if not profiles:

            controls.append(
                panel(
                    ft.Column(
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        ),
                        spacing=12,
                        controls=[
                            ft.Icon(
                                ft.Icons.EXPLORE_OFF,
                                size=48,
                                color=PRIMARY,
                            ),
                            ft.Text(
                                "No new profiles yet",
                                size=18,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            ft.Text(
                                "Check back later when more people "
                                "join AROVI.",
                                color=MUTED,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                    )
                )
            )

        else:

            for person in profiles:

                def like_person(e, person_id=person["id"]):

                    try:

                        mutual = create_like(
                            current_user_id,
                            person_id,
                        )

                        show_discover()

                        notify(
                            "It's a match! You can now message each other."
                            if mutual
                            else "Like sent. If they like you back, "
                                 "you'll match.",
                            GREEN,
                        )

                    except Exception as ex:
                        notify(f"Could not send like: {ex}")

                bio_text = (
                    person.get("bio")
                    or "This member has not added a bio yet."
                )

                controls.append(
                    panel(
                        ft.Column(
                            spacing=10,
                            controls=[
                                ft.Container(
                                    height=135,
                                    bgcolor=LIGHT_PINK,
                                    border_radius=15,
                                    alignment=ft.Alignment.CENTER,
                                    content=ft.Icon(
                                        ft.Icons.PERSON,
                                        size=70,
                                        color=PRIMARY,
                                    ),
                                ),
                                ft.Text(
                                    f"{person.get('name', 'AROVI member')}, "
                                    f"{person.get('age', '')}",
                                    size=21,
                                    weight=ft.FontWeight.BOLD,
                                    color=DARK,
                                ),
                                ft.Row(
                                    spacing=6,
                                    controls=[
                                        ft.Icon(
                                            ft.Icons.LOCATION_ON_OUTLINED,
                                            size=16,
                                            color=PRIMARY,
                                        ),
                                        ft.Text(
                                            person.get("location")
                                            or "Location not provided",
                                            size=12,
                                            color=MUTED,
                                        ),
                                    ],
                                ),
                                ft.Text(
                                    bio_text,
                                    size=13,
                                    color=DARK,
                                ),
                                primary_button(
                                    "LIKE PROFILE",
                                    like_person,
                                ),
                            ],
                        )
                    )
                )

        show_screen(
            make_scrollable(controls),
            navigation=True,
        )

    # ========================================================
    # MATCHES
    # ========================================================

    def show_matches():

        notice = make_notice()

        try:
            matches = get_matches(current_user_id)

        except Exception as ex:
            matches = []
            notice.value = f"Could not load matches: {ex}"

        controls = [
            app_logo(25),
            heading(
                "Your matches",
                "People who have liked you back.",
            ),
            notice,
        ]

        if not matches:

            controls.append(
                panel(
                    ft.Column(
                        horizontal_alignment=(
                            ft.CrossAxisAlignment.CENTER
                        ),
                        spacing=10,
                        controls=[
                            ft.Icon(
                                ft.Icons.FAVORITE_BORDER,
                                size=45,
                                color=PRIMARY,
                            ),
                            ft.Text(
                                "No matches yet",
                                size=18,
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            ft.Text(
                                "Like profiles in Discover. When someone "
                                "likes you back, the match will appear here.",
                                text_align=ft.TextAlign.CENTER,
                                color=MUTED,
                            ),
                        ],
                    )
                )
            )

        else:

            for person in matches:

                def open_match_chat(
                    e,
                    person_id=person["id"],
                ):
                    open_chat(person_id)

                controls.append(
                    panel(
                        ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            controls=[
                                ft.Row(
                                    expand=True,
                                    spacing=12,
                                    controls=[
                                        person_avatar(48),
                                        ft.Column(
                                            expand=True,
                                            spacing=3,
                                            controls=[
                                                ft.Text(
                                                    person.get(
                                                        "name",
                                                        "AROVI member",
                                                    ),
                                                    weight=ft.FontWeight.BOLD,
                                                    color=DARK,
                                                ),
                                                ft.Text(
                                                    f"{person.get('age', '')} "
                                                    f"• {person.get('location') or ''}",
                                                    size=11,
                                                    color=MUTED,
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CHAT,
                                    icon_color=PRIMARY,
                                    on_click=open_match_chat,
                                ),
                            ],
                        )
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

        notice = make_notice()

        try:
            matches = get_matches(current_user_id)

        except Exception as ex:
            matches = []
            notice.value = f"Could not load messages: {ex}"

        controls = [
            app_logo(25),
            heading(
                "Messages",
                "Continue conversations with your matches.",
            ),
            notice,
        ]

        if not matches:

            controls.append(
                panel(
                    ft.Text(
                        "You need a mutual match before you can "
                        "start messaging.",
                        color=MUTED,
                        text_align=ft.TextAlign.CENTER,
                    )
                )
            )

        else:

            for person in matches:

                try:
                    last = get_last_message(
                        current_user_id,
                        person["id"],
                    )
                except Exception:
                    last = None

                preview = (
                    last["message"]
                    if last
                    else "Start a conversation"
                )

                def open_person_chat(
                    e,
                    person_id=person["id"],
                ):
                    open_chat(person_id)

                controls.append(
                    panel(
                        ft.ListTile(
                            leading=person_avatar(45),
                            title=ft.Text(
                                person.get(
                                    "name",
                                    "AROVI member",
                                ),
                                weight=ft.FontWeight.BOLD,
                                color=DARK,
                            ),
                            subtitle=ft.Text(
                                preview,
                                max_lines=1,
                            ),
                            trailing=ft.Icon(
                                ft.Icons.CHEVRON_RIGHT,
                                color=PRIMARY,
                            ),
                            on_click=open_person_chat,
                        )
                    )
                )

        show_screen(
            make_scrollable(controls),
            navigation=True,
        )

    # ========================================================
    # PRIVATE CHAT
    # ========================================================

    def open_chat(other_user_id):

        try:

            person = get_profile(other_user_id)

            if (
                not person
                or not is_matched(
                    current_user_id,
                    other_user_id,
                )
            ):
                show_messages()
                return

        except Exception:
            show_messages()
            return

        notice = make_notice()

        message_input = ft.TextField(
            hint_text="Write a message...",
            expand=True,
            text_size=14,
        )

        chat_column = ft.Column(
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=10,
        )

        def refresh_chat():

            chat_column.controls.clear()

            try:

                messages = get_conversation(
                    current_user_id,
                    other_user_id,
                )

            except Exception as ex:

                chat_column.controls.append(
                    ft.Text(
                        f"Could not load conversation: {ex}",
                        color=PRIMARY,
                    )
                )
                return

            if not messages:

                chat_column.controls.append(
                    ft.Container(
                        padding=20,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Text(
                            "Say hello and start your conversation.",
                            color=MUTED,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                )

            for msg in messages:

                mine = (
                    str(msg["sender_id"])
                    == str(current_user_id)
                )

                chat_column.controls.append(
                    ft.Row(
                        alignment=(
                            ft.MainAxisAlignment.END
                            if mine
                            else ft.MainAxisAlignment.START
                        ),
                        controls=[
                            ft.Container(
                                content=ft.Text(
                                    msg["message"],
                                    color=WHITE if mine else DARK,
                                    size=13,
                                ),
                                padding=12,
                                bgcolor=(
                                    PRIMARY if mine else LIGHT_PINK
                                ),
                                border_radius=14,
                                width=250,
                            )
                        ],
                    )
                )

        def send_message(e):

            message = (message_input.value or "").strip()

            if not message:
                return

            if len(message) > 2000:
                notify(
                    "Messages cannot exceed 2,000 characters."
                )
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

                refresh_chat()
                page.update()

            except Exception as ex:
                notify(f"Message error: {ex}")

        refresh_chat()

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
                                    on_click=lambda e: show_messages(),
                                ),
                                person_avatar(42),
                                ft.Column(
                                    expand=True,
                                    spacing=2,
                                    controls=[
                                        ft.Text(
                                            person.get(
                                                "name",
                                                "AROVI member",
                                            ),
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
                        content=chat_column,
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

    # ========================================================
    # PROFILE
    # ========================================================

    def show_profile():

        try:
            user = get_profile(current_user_id)

        except Exception as ex:

            show_screen(
                ft.Column(
                    controls=[
                        app_logo(25),
                        heading("My profile"),
                        ft.Text(str(ex), color=PRIMARY),
                    ]
                ),
                navigation=True,
            )
            return

        if not user:
            open_dashboard()
            return

        notice = make_notice()

        name = field("Full name")
        name.value = user.get("name", "")

        age = field(
            "Age",
            keyboard=ft.KeyboardType.NUMBER,
        )
        age.value = str(user.get("age", ""))

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
        )

        gender = ft.Dropdown(
            label="Gender",
            value=user.get("gender"),
            options=[
                ft.dropdown.Option("Man"),
                ft.dropdown.Option("Woman"),
                ft.dropdown.Option("Non-binary"),
            ],
        )

        interested = ft.Dropdown(
            label="Interested in",
            value=user.get("interested_in"),
            options=[
                ft.dropdown.Option("Men"),
                ft.dropdown.Option("Women"),
                ft.dropdown.Option("Everyone"),
            ],
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
                    "bio": (bio.value or "").strip(),
                    "gender": gender.value,
                    "interested_in": interested.value,
                    "updated_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }).eq(
                    "id",
                    str(current_user_id),
                ).execute()

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

            show_welcome()

        content = make_scrollable([
            app_logo(25),
            heading(
                "My profile",
                "Keep your information up to date.",
            ),
            panel(
                ft.Column(
                    horizontal_alignment=(
                        ft.CrossAxisAlignment.CENTER
                    ),
                    spacing=8,
                    controls=[
                        person_avatar(84),
                        ft.Text(
                            user.get("email") or "",
                            color=MUTED,
                            size=12,
                        ),
                    ],
                )
            ),
            name,
            age,
            gender,
            interested,
            location,
            bio,
            notice,
            primary_button(
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
