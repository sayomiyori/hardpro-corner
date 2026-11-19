"""Prepare the host key/CSR or enroll the 1C password on the Windows reader host."""
import argparse
import getpass
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def csr(directory):
    keypath = directory / "server.key"
    if keypath.exists():
        key = serialization.load_pem_private_key(keypath.read_bytes(),password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        with keypath.open("xb") as file:
            file.write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"hardpro-onec-reader")])
    request = x509.CertificateSigningRequestBuilder().subject_name(name).sign(key,hashes.SHA256())
    (directory/"server.csr").write_bytes(request.public_bytes(serialization.Encoding.PEM))


def enroll(directory):
    import win32crypt
    from win32com.client import Dispatch
    password = getpass.getpass("Пароль служебной записи HP_Manager_Read: ")
    if not password:
        raise SystemExit("Пустой пароль")
    quoted = password.replace('"','""')
    db = Dispatch("V83.COMConnector").Connect(f'Srvr="Hoster";Ref="base";Usr="HP_Manager_Read";Pwd="{quoted}";WA=0;')
    if db.UserName() != "HP_Manager_Read":
        raise SystemExit("1С подтвердила другую запись")
    # LocalMachine позволяет задаче запускаться до входа оператора. Файл защищает ACL каталога.
    encrypted = win32crypt.CryptProtectData(password.encode(),"HARD-PRO manager reader",None,None,None,4)
    pending = directory/"password.pending"
    with pending.open("xb") as f:
        f.write(encrypted)
    os.replace(pending,directory/"password.dpapi")
    print("Учётная запись подтверждена 1С. Секрет зашифрован на этом сервере.")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("action",choices=("csr","enroll"))
    ap.add_argument("private_dir",type=Path,help="Каталог с закрытыми Windows ACL")
    args=ap.parse_args()
    if not args.private_dir.is_dir():
        raise SystemExit("Сначала создайте каталог с закрытыми ACL")
    (csr if args.action=="csr" else enroll)(args.private_dir)


if __name__=="__main__":
    main()
