"""WAHA-Webhook-Selbstheilung: die Entscheidung ist rein. Genau die Fälle, die uns
wochenlang WhatsApp gekostet haben (kein Hook, falsches Secret) müssen erkannt werden."""
from app import waha_hooks as w

SECRET = "s3cret-value-long-enough"


def _session(webhooks=None, status="WORKING"):
    cfg = {"webhooks": webhooks} if webhooks is not None else None
    return {"name": "default", "status": status, "config": cfg}


def _hook(secret=SECRET, url=w.HOOK_URL, events=("message", "message.any")):
    return {"url": url, "events": list(events),
            "customHeaders": [{"name": "X-Astra-Secret", "value": secret}]}


def test_missing_webhook_is_detected():
    # Der Ursprungsfehler: config: null
    assert w.needs_update(_session(None), SECRET) == (True, "kein Webhook gesetzt")
    assert w.needs_update(_session([]), SECRET)[0] is True


def test_correct_webhook_needs_nothing():
    assert w.needs_update(_session([_hook()]), SECRET) == (False, "ok")


def test_secret_mismatch_is_detected():
    need, why = w.needs_update(_session([_hook(secret="alt")]), SECRET)
    assert need is True and "Secret" in why


def test_hook_pointing_elsewhere_is_detected():
    assert w.needs_update(_session([_hook(url="http://x/y")]), SECRET)[0] is True


def test_missing_events_are_detected():
    assert w.needs_update(_session([_hook(events=("message",))]), SECRET)[0] is True


def test_stopped_session_is_left_alone():
    # Nicht an einer gestoppten/kaputten Session herumschrauben.
    assert w.needs_update(_session(None, status="STOPPED"), SECRET)[0] is False


def test_no_session_is_not_an_error():
    assert w.needs_update(None, SECRET)[0] is False


def test_desired_webhook_shape():
    d = w.desired_webhook(SECRET)
    assert d["url"] == w.HOOK_URL and d["customHeaders"][0]["value"] == SECRET


def test_placeholder_secrets_are_flagged():
    for bad in ("", "dev-secret", "change-me-too", "CHANGE-ME", "short"):
        assert w.secret_is_placeholder(bad) is True
    assert w.secret_is_placeholder("9f2c7e1a5b8d4c3e6f0a1b2c3d4e5f60") is False
