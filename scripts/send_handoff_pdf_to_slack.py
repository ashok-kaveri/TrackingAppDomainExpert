#!/usr/bin/env python3
"""Send an already-generated handoff PDF to Slack.

Defaults to a DM to the owner (SLACK_HANDOFF_DM_EMAIL, or --email) so the PDF
can be eyeballed before it reaches a team channel.

Nothing is sent without --yes. Without it the script prints exactly what it
would send and exits, so the target can be confirmed first.

    # dry run — shows target, filename, size, comment
    python3 scripts/send_handoff_pdf_to_slack.py --pdf data/handoff_docs/Tracking_App_v1.0.27_Support_Guide.pdf

    # send after approval
    python3 scripts/send_handoff_pdf_to_slack.py --pdf data/handoff_docs/Tracking_App_v1.0.27_Support_Guide.pdf --yes

    # send to the team channel (bare --channel = qa_members_internal)
    python3 scripts/send_handoff_pdf_to_slack.py --pdf ... --channel --yes

    # or name any other channel the bot is in
    python3 scripts/send_handoff_pdf_to_slack.py --pdf ... --channel qa-team --yes
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_DM_EMAIL = os.getenv("SLACK_HANDOFF_DM_EMAIL", "ashok@pluginhive.com")
# Team destination for release handoff docs. `--channel` with no value uses this.
DEFAULT_CHANNEL = os.getenv("SLACK_HANDOFF_CHANNEL", "qa_members_internal")


def _resolve_channel(name_or_id: str) -> tuple[str, str, str]:
    """Return (channel_id, display_label, error) for a channel name or id."""
    from pipeline.slack_client import list_slack_channels

    raw = (name_or_id or "").strip().lstrip("#")
    if raw.startswith(("C", "G")) and raw.isupper() and len(raw) >= 9:
        return raw, raw, ""
    channels, err, note = list_slack_channels()
    if err:
        return "", "", err
    for channel in channels or []:
        if channel.get("name", "").lower() == raw.lower():
            prefix = "🔒" if channel.get("is_private") else "#"
            return channel["id"], f"{prefix}{channel['name']}", ""
    return "", "", note or f"Channel not found: {raw}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a handoff PDF to Slack.")
    parser.add_argument("--pdf", required=True, help="Path to the PDF to send.")
    parser.add_argument("--title", default="", help="Slack file title (defaults to filename).")
    parser.add_argument("--comment", default="", help="Message posted with the file.")
    parser.add_argument("--channel", nargs="?", const=DEFAULT_CHANNEL, default="",
                        help=f"Channel name or id. Bare --channel uses {DEFAULT_CHANNEL}. "
                             "Omit entirely to DM instead.")
    parser.add_argument("--user", default="", help="Slack user id to DM.")
    parser.add_argument("--email", default=DEFAULT_DM_EMAIL,
                        help=f"Email to resolve for the DM (default {DEFAULT_DM_EMAIL}).")
    parser.add_argument("--yes", action="store_true",
                        help="Actually send. Without this the script only reports the target.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    import config  # noqa: F401  — loads .env so SLACK_BOT_TOKEN is available

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"❌ PDF not found: {pdf_path}")
        return 1
    pdf_bytes = pdf_path.read_bytes()
    title = args.title or pdf_path.stem.replace("_", " ")
    comment = args.comment or f"📘 {title}"

    from pipeline.slack_client import (
        lookup_slack_user_by_email,
        upload_file_to_slack_channel,
        upload_file_to_slack_user,
    )

    if args.channel:
        channel_id, label, err = _resolve_channel(args.channel)
        if err:
            print(f"❌ {err}")
            return 1
        target, send = label, lambda: upload_file_to_slack_channel(
            channel_id=channel_id, filename=pdf_path.name, file_bytes=pdf_bytes,
            title=title, initial_comment=comment,
        )
    else:
        user_id = args.user.strip()
        label = user_id
        if not user_id:
            user_id, err = lookup_slack_user_by_email(args.email)
            label = args.email
            if not user_id:
                # users.lookupByEmail needs the users:read.email scope. Fall back to
                # a name search on the email local-part, which only needs users:read.
                from pipeline.slack_client import search_slack_users

                needle = args.email.split("@")[0]
                found, search_err = search_slack_users(needle)
                if len(found or []) == 1:
                    user_id = found[0]["id"]
                    label = f"{found[0].get('real_name') or needle} ({args.email})"
                elif found:
                    print(f"❌ {len(found)} Slack users match '{needle}'. Re-run with --user <id>:")
                    for user in found[:10]:
                        print(f"   {user['id']}  {user.get('real_name', '')}")
                    return 1
                else:
                    print(f"❌ Could not resolve {args.email}: {err or search_err or 'no match'}")
                    return 1
        target, send = f"DM to {label}", lambda: upload_file_to_slack_user(
            user_id=user_id, filename=pdf_path.name, file_bytes=pdf_bytes,
            title=title, initial_comment=comment,
        )

    print(f"Target   : {target}")
    print(f"File     : {pdf_path.name}  ({len(pdf_bytes) / 1024:.0f} KB)")
    print(f"Title    : {title}")
    print(f"Comment  : {comment}")
    if not args.yes:
        print("\nDry run — nothing sent. Re-run with --yes to send.")
        return 0

    result = send()
    if result.get("ok"):
        print(f"\n✅ Sent to Slack ({target}), file id {result.get('file_id', '')}")
        return 0
    print(f"\n❌ Slack upload failed: {result.get('error')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
