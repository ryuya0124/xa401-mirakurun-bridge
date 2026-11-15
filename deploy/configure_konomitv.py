"""Generate a separate local KonomiTV configuration. Does not modify its code."""
import argparse
import ipaddress
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--konomitv', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--encoder', choices=['FFmpeg', 'NVEncC', 'QSVEncC', 'VCEEncC'], default='FFmpeg')
    args = parser.parse_args()
    root, runtime = args.konomitv.resolve(), args.runtime.resolve()
    destination = root / 'config.yaml'
    if destination.exists():
        parser.error('config.yaml already exists; edit it directly instead of overwriting it')
    for directory in ('tls', 'recordings', 'captures'):
        (runtime / directory).mkdir(parents=True, exist_ok=True)
    key_path, cert_path = runtime / 'tls/localhost.key', runtime / 'tls/localhost.crt'
    if not key_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local KonomiTV')])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
                    x509.IPAddress(ipaddress.ip_address('127.0.0.77'))]), critical=False)
                .sign(key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        if key_path.stat().st_mode:
            key_path.chmod(0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if not cert_path.exists():
        parser.error('Existing key has no certificate; repair the local TLS directory')
    text = (root / 'config.example.yaml').read_text(encoding='utf-8')
    replacements = {
        "backend: 'EDCB'": "backend: 'Mirakurun'",
        "encoder: 'FFmpeg'": f"encoder: '{args.encoder}'",
        'custom_https_certificate: null': f"custom_https_certificate: '{cert_path.as_posix()}'",
        'custom_https_private_key: null': f"custom_https_private_key: '{key_path.as_posix()}'",
        'E:\\TV-Record': (runtime / 'recordings').as_posix(),
        'E:\\TV-Capture': (runtime / 'captures').as_posix(),
    }
    for before, after in replacements.items():
        if before not in text:
            parser.error('Unexpected KonomiTV config template; check its version')
        text = text.replace(before, after)
    destination.write_text(text, encoding='utf-8')
    shutil.copyfile(Path(__file__).with_name('xa401-original.html'), root / 'client/dist/xa401-original.html')
    print('Configured Mirakurun backend with local TLS; certificate trust was not changed.')


if __name__ == '__main__':
    main()
