"""Flask app that provisions a temporary 360Learning trial user via a webform.

Flow per submission:
  1. AE supplies: AE email (firstname.lastname@360learning.com), customer name, language.
  2. We build the trial user's email as `firstname.lastname#<customerslug>@360learning.com`.
  3. Map language -> group ID, and create the user as a `learner` in that group
     with `toBeDeactivatedAt = now + EXPIRY_DAYS` (360Learning auto-deactivates).
  4. Add the `admin` role to the same group via POST /v2/groups/{id}/members,
     so the user is both learner AND group admin.
  5. For each managee userId in MANAGEE_USER_IDS, POST /v2/users/{managee}/managers
     with the new user as the manager.

Docs:
  Auth:           https://360learning.readme.io/docs/authentication
  Create user:    https://360learning.readme.io/docs/create-a-user-20-migration-guide
  Add role/group: POST /api/v2/groups/{groupId}/members  (Add a user's role)
  Add manager:    POST /api/v2/users/{userId}/managers   (Add a manager to a user)
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request

load_dotenv()

# --- Secrets / config (env) -----------------------------------------------

CLIENT_ID = os.environ["THREESIXTY_CLIENT_ID"]
CLIENT_SECRET = os.environ["THREESIXTY_CLIENT_SECRET"]
BASE_URL = os.environ.get("THREESIXTY_BASE_URL", "https://app.360learning.com/api/v2").rstrip("/")
EXPIRY_DAYS = int(os.environ.get("EXPIRY_DAYS", "3"))

GROUP_IDS: dict[str, str] = {
    "en": os.environ["ENGLISH_GROUP_ID"],
    "fr": os.environ["FRENCH_GROUP_ID"],
    "de": os.environ["GERMAN_GROUP_ID"],
}

LANGUAGE_LABELS = {"en": "English", "fr": "French", "de": "German"}

# Random first/last name pools used to populate the user's displayed name
# in 360Learning. The email local-part is still derived from the AE email.
NAME_POOLS: dict[str, dict[str, list[str]]] = {
    "en": {
        "first": ["Oliver", "Emma", "Liam", "Ava", "Noah", "Mia", "Lucas", "Sophia", "Henry", "Isla"],
        "last":  ["Smith", "Johnson", "Brown", "Taylor", "Wilson", "Davies", "Evans", "Walker", "Hughes", "Green"],
    },
    "fr": {
        "first": ["Lucas", "Emma", "Hugo", "Léa", "Louis", "Chloé", "Jules", "Manon", "Arthur", "Camille"],
        "last":  ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Petit", "Durand", "Leroy", "Moreau", "Simon"],
    },
    "de": {
        "first": ["Maximilian", "Mia", "Paul", "Hannah", "Elias", "Emma", "Ben", "Sofia", "Felix", "Lina"],
        "last":  ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Hoffmann", "Schäfer"],
    },
}


def random_display_name(lang: str) -> tuple[str, str]:
    pool = NAME_POOLS[lang]
    return random.choice(pool["first"]), random.choice(pool["last"])

# --- Public, non-secret config (lives in source, committed to GitHub) -----

# 360Learning user _id values. The newly-created trial user will be assigned
# as a manager of every user in this list.
# Look these up once via GET /api/v2/users in your 360Learning instance.
MANAGEE_USER_IDS: list[str] = [
    # "5be2b954b44a1b6e3526e091",
    # "5be2b954b44a1b6e3526e092",
]


# --- Token cache ----------------------------------------------------------

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def get_access_token() -> str:
    """Return a cached bearer token, refreshing ~1 min before expiry."""
    with _token_lock:
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        resp = requests.post(
            f"{BASE_URL}/oauth2/token",
            json={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            headers={"accept": "application/json", "content-type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        return _token_cache["access_token"]


def api_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "360-api-version": "v2.0",
        "accept": "application/json",
        "content-type": "application/json",
    }


# --- Input parsing --------------------------------------------------------

AE_EMAIL_RE = re.compile(r"^([a-z]+)\.([a-z]+)@360learning\.com$", re.IGNORECASE)


def parse_ae_email(ae_email: str) -> tuple[str, str]:
    """Extract (first_name, last_name) from firstname.lastname@360learning.com."""
    m = AE_EMAIL_RE.match(ae_email.strip())
    if not m:
        raise ValueError("AE email must be firstname.lastname@360learning.com")
    return m.group(1).lower(), m.group(2).lower()


def slugify_customer(name: str) -> str:
    """Lowercase + strip non-alphanumeric. 'Acme Inc.' -> 'acmeinc'."""
    slug = re.sub(r"[^a-z0-9]", "", name.lower())
    if not slug:
        raise ValueError("Customer name must contain at least one letter or digit")
    return slug


# --- 360Learning API operations ------------------------------------------

def create_user(*, mail: str, first: str, last: str, lang: str, group_id: str) -> dict[str, Any]:
    deactivate_at = (datetime.now(timezone.utc) + timedelta(days=EXPIRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "mail": mail,
        "username": mail.split("@", 1)[0],
        "firstName": first,
        "lastName": last,
        "lang": lang,
        "membership": {"role": "learner", "groupId": group_id},
        "toBeDeactivatedAt": deactivate_at,
    }
    resp = requests.post(f"{BASE_URL}/users", json=payload, headers=api_headers(), timeout=20)
    if not resp.ok:
        raise RuntimeError(f"create_user failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    body.setdefault("_local", {})["deactivateAt"] = deactivate_at
    return body


def grant_group_admin(*, user_id: str, group_id: str) -> None:
    """Add the `admin` role for this user on the given group."""
    payload = {"userId": user_id, "role": "admin"}
    resp = requests.post(
        f"{BASE_URL}/groups/{group_id}/members",
        json=payload,
        headers=api_headers(),
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"grant_group_admin failed ({resp.status_code}): {resp.text}")


def add_as_manager_of(*, manager_user_id: str, managee_user_id: str) -> None:
    """Make `manager_user_id` a manager of `managee_user_id`."""
    payload = {"managerId": manager_user_id}
    resp = requests.post(
        f"{BASE_URL}/users/{managee_user_id}/managers",
        json=payload,
        headers=api_headers(),
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(
            f"add_as_manager_of({managee_user_id}) failed ({resp.status_code}): {resp.text}"
        )


def provision_trial_user(*, ae_email: str, customer_name: str, lang: str) -> dict[str, Any]:
    if lang not in GROUP_IDS:
        raise ValueError(f"Unsupported language '{lang}'")
    if not GROUP_IDS[lang]:
        raise ValueError(f"No group ID configured for language '{lang}' — set it in .env")

    ae_first, ae_last = parse_ae_email(ae_email)
    customer_slug = slugify_customer(customer_name)
    trial_email = f"{ae_first}.{ae_last}#{customer_slug}@360learning.com"

    display_first, display_last = random_display_name(lang)

    user = create_user(
        mail=trial_email,
        first=display_first,
        last=display_last,
        lang=lang,
        group_id=GROUP_IDS[lang],
    )
    user_id = user["_id"]

    grant_group_admin(user_id=user_id, group_id=GROUP_IDS[lang])

    manager_results: list[dict[str, Any]] = []
    for managee_id in MANAGEE_USER_IDS:
        try:
            add_as_manager_of(manager_user_id=user_id, managee_user_id=managee_id)
            manager_results.append({"managee": managee_id, "ok": True})
        except Exception as exc:
            manager_results.append({"managee": managee_id, "ok": False, "error": str(exc)})

    return {
        "userId": user_id,
        "trialEmail": trial_email,
        "firstName": display_first,
        "lastName": display_last,
        "language": lang,
        "groupId": GROUP_IDS[lang],
        "deactivateAt": user["_local"]["deactivateAt"],
        "managees": manager_results,
    }


# --- Flask app ------------------------------------------------------------

app = Flask(__name__)


@app.get("/")
def form() -> str:
    return render_template("form.html", languages=LANGUAGE_LABELS)


@app.post("/submit")
def submit() -> tuple[str, int]:
    ae_email = (request.form.get("ae_email") or "").strip()
    customer_name = (request.form.get("customer_name") or "").strip()
    lang = (request.form.get("language") or "").strip().lower()

    try:
        result = provision_trial_user(ae_email=ae_email, customer_name=customer_name, lang=lang)
    except ValueError as exc:
        return render_template("form.html", languages=LANGUAGE_LABELS, error=str(exc)), 400
    except Exception as exc:
        return render_template("form.html", languages=LANGUAGE_LABELS, error=str(exc)), 502

    return render_template("form.html", languages=LANGUAGE_LABELS, result=result), 200


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_PORT", "5000")),
        debug=False,
    )
