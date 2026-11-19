"""Configure loopback user services and keep setup secrets private."""
from pathlib import Path
import secrets
import os
import json

os.umask(0o077)
base = Path.home() / ".local/share/hardpro/catalog-web"
unit_dir = Path.home() / ".config/systemd/user"
def write(name, data):
    file = base / name
    if not file.exists():
        file.write_text(data)
    file.chmod(0o600)

def token():
    return secrets.token_urlsafe(36)

write("access.json", json.dumps({k: token() for k in ["manager","buyer","webui","admin","flowjwt","flowrefresh","session"]}))
write("relay.secret", token())
keys = json.loads((base/"access.json").read_text())
def env(name, values):
    (base / (name + ".env")).write_text("".join(f"{k}={v}\n" for k,v in values.items()))

env("catalog", {"MANAGER_TOKEN":keys["manager"], "BUYER_TOKEN":keys["buyer"]})
suggestions = [
    {"title":["Найти товар","по модели и размеру"],"content":"Какие есть тайские носки OxxFire?"},
    {"title":["Проверить остаток","по одному складу"],"content":"Сколько чёрных тайских носков OxxFire размера M свободно в Симферополе?"},
]
env("openwebui", {"DEFAULT_PROMPT_SUGGESTIONS": "'" + json.dumps(suggestions, ensure_ascii=False) + "'","WEBUI_AUTH":"False", "WEBUI_SECRET_KEY":keys["webui"],
    "ENABLE_PERSISTENT_CONFIG":"False", "ENABLE_OPENAI_API":"True", "ENABLE_OLLAMA_API":"False",
    "OPENAI_API_BASE_URLS":"http://127.0.0.1:8767/manager/v1", "OPENAI_API_KEYS":keys["manager"],
    "DATA_DIR":base/"openwebui/data", "DEFAULT_MODELS":"hardpro-manager","DEFAULT_LOCALE":"ru-RU",
    "OFFLINE_MODE":"True","HF_HUB_OFFLINE":"1","ENABLE_VERSION_UPDATE_CHECK":"False",
    "ENABLE_TITLE_GENERATION":"False","ENABLE_FOLLOW_UP_GENERATION":"False","ENABLE_TAGS_GENERATION":"False",
    "RAG_EMBEDDING_MODEL_AUTO_UPDATE":"False","DO_NOT_TRACK":"true","SCARF_NO_ANALYTICS":"true",
    "ANONYMIZED_TELEMETRY":"false","ENABLE_SIGNUP":"False","ENABLE_PLUGINS":"False","ENABLE_CODE_EXECUTION":"False",
    "CORS_ALLOW_ORIGIN":"http://127.0.0.1:8765;http://localhost:8765"})
node = base/"flowise/node-v24.22.3-linux-x64/bin"
env("flowise",{"HOST":"127.0.0.1","PORT":"8768", "PATH":f"{node}:/usr/local/bin:/usr/bin:/bin",
    "DATABASE_PATH":base/"flowise/data","SECRETKEY_PATH":base/"flowise/data", "BLOB_STORAGE_PATH":base/"flowise/storage",
    "LOG_PATH":base/"logs/flowise","DISABLE_FLOWISE_TELEMETRY":"true", "HTTP_SECURITY_CHECK":"true",
    "JWT_AUTH_TOKEN_SECRET":keys["flowjwt"], "JWT_REFRESH_TOKEN_SECRET":keys["flowrefresh"],
    "EXPRESS_SESSION_SECRET":keys["session"]})
for d in ["openwebui/data","flowise/data","flowise/storage","logs/flowise"]:
    (base/d).mkdir(parents=True, exist_ok=True)

uvicorn = base.parent/"vllm/bin/uvicorn"
commands={"catalog":f"{uvicorn} catalog_api:app --host 127.0.0.1 --port 8767 --no-access-log",
    "openwebui":f"{base}/openwebui/venv/bin/open-webui serve --host 127.0.0.1 --port 8765",
    "flowise":f"{node}/node {base}/flowise/node_modules/flowise/bin/run start",
    "widget":f"{uvicorn} widget:app --host 127.0.0.1 --port 8766 --no-access-log"}
for name, command in commands.items():
    unit=(f"[Unit]\nDescription=HARD-PRO local {name}\n[Service]\nWorkingDirectory={base}\n"
          f"EnvironmentFile={base}/{name}.env\nExecStart={command}\nRestart=on-failure\nRestartSec=10\n"
          "UMask=0077\nNoNewPrivileges=true\n[Install]\nWantedBy=default.target\n")
    (unit_dir/f"hardpro-{name}.service").write_text(unit)
