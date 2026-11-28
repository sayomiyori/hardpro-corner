"""Adapter for a trusted admin panel. Authentication and CSRF checks belong to its routes."""
import json
import subprocess


def remote(worker_command, request):
    if not isinstance(request, dict) or request.get("mode") not in ("status", "plan", "apply"):
        return 400, {"error": "Некорректная операция профиля."}
    # The operator configures the fixed local/SSH command. Only JSON is sent on stdin.
    try:
        result = subprocess.run(worker_command, input=json.dumps(request).encode(),
                                capture_output=True, timeout=45)
        payload = json.loads(result.stdout)
        if result.returncode and not payload.get("error"):
            raise ValueError("Incomplete worker response")
        return (409 if "error" in payload else 200), payload
    except (ValueError, OSError, subprocess.SubprocessError):
        return 503, {"error": "Исполнитель недоступен. Проверьте статус задания."}
