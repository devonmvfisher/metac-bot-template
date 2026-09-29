"""Bounded rationale blocks and fail-open season-end decisions."""
from datetime import datetime, timezone
import os
import sys
from . import config
from .targets import utc


def cap_rationales(summary, rationales):
    try:
        if not isinstance(summary, str) or not isinstance(rationales, (list, tuple)):
            return []
        if not all(isinstance(text, str) for text in rationales):
            return []
        per_run = 1500 if len(rationales) >= 4 else 2500
        room = 10000 - len(summary) - 2
        out = []
        for i, text in enumerate(rationales, 1):
            head = f"RUN {i}\n"
            item = (head + text)[:per_run]
            if out:
                room -= 2
            item = item[:max(0, room)]
            if len(item) <= len(head):
                break
            out.append(item)
            room -= len(item)
        return out
    except Exception:
        return []


def season_open(now, env=None):
    try:
        default = getattr(config, "SEASON_END_UTC", None)
        try:
            end = utc((env.get("SEASON_END_UTC") or default) if env is not None else default)
        except Exception:
            end = utc(default)
        return utc(now) < end
    except Exception:
        return True


def season_over_actions(now, env, issue_titles):
    if season_open(now, env):
        return {"forecast": True, "heartbeat": True, "post_season_over": False}
    try:
        post = "[BOT ALERT] SEASON_OVER" not in issue_titles
    except Exception:
        post = False
    return {"forecast": False, "heartbeat": False, "post_season_over": post}


def main(argv=None, env=None, now=None):
    opened = True
    try:
        argv = sys.argv[1:] if argv is None else argv
        env = os.environ if env is None else env
        now = datetime.now(timezone.utc) if now is None else now
        if argv == ["--season-open"]:
            utc(now)
            if str(env.get("SEASON_END_UTC") or "").strip():
                utc(env["SEASON_END_UTC"])
            opened = season_open(now, env)
    except Exception:
        opened = True
    print("season_open=" + str(opened).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
