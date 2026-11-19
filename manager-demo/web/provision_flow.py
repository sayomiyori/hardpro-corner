"""Register a local owner, credential and a chatflow through the Flowise admin API."""
import json
import os
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

os.umask(0o077)
base = Path.home() / ".local/share/hardpro/catalog-web"
keys=json.loads((base/"access.json").read_text())
email="catalog-owner@example.invalid"
client=build_opener(HTTPCookieProcessor(CookieJar()))
def call(path, payload=None):
    req=Request("http://127.0.0.1:8768/api/v1/"+path,
                data=None if payload is None else json.dumps(payload).encode(),
                headers={"Content-Type":"application/json","x-request-from":"internal"})
    try:
        with client.open(req,timeout=30) as r:
            return json.load(r)
    except HTTPError as err:
        # Only the error message is logged, never credentials or session cookies.
        reason=json.loads(err.read()).get("message",err.reason)
        raise RuntimeError(f"{path} ({err.code}) {str(reason)[:220]}") from None

try:
    call("auth/login", {"email":email,"password":keys["admin"]})
except RuntimeError:
    call("account/register", {"user":{"name":"Catalogue pilot","email":email,"credential":keys["admin"]}})
    call("auth/login", {"email":email,"password":keys["admin"]})
cred=next((c for c in call("credentials") if c["name"]=="Catalogue buyer"),None)
if not cred:
    cred=call("credentials",{"name":"Catalogue buyer","credentialName":"openAIApi",
                             "plainDataObj":{"openAIApiKey":keys["buyer"]}})
tpl=json.loads((base/"Conversation Chain.json").read_text())
for node in tpl["nodes"]:
    data=node["data"]
    if data["name"]=="chatOpenAI":
        data["credential"]=cred["id"]
        data["inputs"].update(modelName="hardpro-shop",basepath="http://127.0.0.1:8767/buyer/v1",
                              allowImageUploads=False,streaming=False,temperature=0)
    elif data["name"]=="conversationChain":
        data["inputs"]["systemMessagePrompt"]="Помощник покупателя по каталогу HARD-PRO."
name="HARD-PRO · Каталог покупателя"
flows=call("chatflows")
flow=next((f for f in flows if f["name"]==name),None)
if not flow:
    flow=call("chatflows",{"name":name,"flowData":json.dumps({k:tpl[k] for k in ("nodes","edges")}),
                           "type":"CHATFLOW","deployed":True,"isPublic":False})
(base/"widget.env").write_text("BUYER_FLOW="+flow["id"]+"\n")
print("Buyer chatflow ready")
