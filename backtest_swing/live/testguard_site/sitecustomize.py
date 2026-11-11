"""시험이 띄운 자식 프로세스에도 시험 보호 (testguard.py). PYTHONPATH 로 시험 때만 읽힌다. 운영에서는 이 폴더가 경로에 없다."""
import os
import sys

if os.environ.get("LIVE_TEST_GUARD") == "1":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import testguard
        testguard.install_child()
    finally:
        sys.path.pop(0)
