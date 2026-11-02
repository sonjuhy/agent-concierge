#!/usr/bin/env python3
"""cli_runner 테스트용 가짜 agy/claude. FAKE_MODE 환경 변수로 동작을 고른다."""

import json
import os
import subprocess
import sys
import time

mode = os.environ.get("FAKE_MODE", "agy_ok")
argv = sys.argv[1:]
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""

if mode == "agy_ok":
    prompt = argv[argv.index("--print") + 1]
    print(json.dumps({
        "conversation_id": "conv-1",
        "status": "SUCCESS",
        "response": f"echo:{prompt}\n",
        "usage": {"total_tokens": 3},
        "argv": argv,
        "cwd": os.getcwd(),
        "env": {k: os.environ.get(k) for k in ("HOME", "WRAPPED")},
    }))
elif mode == "agy_error":
    print(json.dumps({"conversation_id": "", "status": "ERROR", "response": "", "error": "boom"}))
elif mode == "claude_ok":
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "sess-1",
        "result": f"echo:{stdin}",
        "total_cost_usd": 0.01,
        "usage": {"output_tokens": 2},
        "argv": argv,
        "cwd": os.getcwd(),
    }))
elif mode == "bad_json":
    print("not json")
elif mode == "exit_nonzero":
    print(json.dumps({"status": "SUCCESS", "response": "x"}))
    print("fatal", file=sys.stderr)
    sys.exit(3)
elif mode == "hang_with_child":
    # 손자 프로세스까지 남기는 행(hang) 상황 재현
    child = subprocess.Popen(["sleep", "300"])
    with open(os.environ["FAKE_PIDFILE"], "w") as f:
        f.write(str(child.pid))
    time.sleep(300)
