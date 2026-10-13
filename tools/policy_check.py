#!/usr/bin/env python3
"""Run a local, dry-run authorization decision and preview pending work."""

from __future__ import print_function

import argparse
import json
import sys
from pathlib import Path

try:
    from authorization import (
        DENY,
        PENDING_APPROVAL,
        ALLOW,
        ApprovalQueue,
        AuthorizationError,
        PolicyEngine,
        load_policy,
    )
except ImportError:
    _TOOLS = Path(__file__).resolve().parent
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    from authorization import (
        DENY,
        PENDING_APPROVAL,
        ALLOW,
        ApprovalQueue,
        AuthorizationError,
        PolicyEngine,
        load_policy,
    )


def _load_json(path):
    try:
        text = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, ValueError) as error:
        raise AuthorizationError("cannot read JSON request: " + str(error))
    if not isinstance(value, dict):
        raise AuthorizationError("request must be a JSON object")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",), help="local policy operation")
    parser.add_argument("--dry-run", action="store_true", help="required; never executes the request")
    parser.add_argument("--config", required=True, help="local authorization policy JSON")
    parser.add_argument("--request", required=True, help="request JSON path, or - for stdin")
    parser.add_argument("--approval", help="optional approval JSON; default verifier never trusts raw input")
    parser.add_argument(
        "--queue-file",
        help="optional local 0600 JSON pending queue; only PENDING_APPROVAL is enqueued",
    )
    parser.add_argument("--now", help="optional ISO-8601 time for deterministic testing")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.dry_run:
        print("policy_check: REFUSED; --dry-run is required", file=sys.stderr)
        return 2
    try:
        engine = PolicyEngine(load_policy(args.config))
        request = _load_json(args.request)
        approval = _load_json(args.approval) if args.approval else None
        result = engine.evaluate(request, approval=approval, now=args.now)
        if args.queue_file and result["decision"] == PENDING_APPROVAL:
            queue = ApprovalQueue(args.queue_file)
            inserted = queue.enqueue(result)
            result["queue"] = {
                "status": "enqueued" if inserted else "already_present",
                "pending_count": len(queue.pending(now=args.now)),
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return {ALLOW: 0, PENDING_APPROVAL: 3, DENY: 4}[result["decision"]]
    except (AuthorizationError, KeyError, TypeError, ValueError) as error:
        print("policy_check: ERROR", file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
