"""Reissue the locally generated TLS certificate with specified LAN IP SANs."""
import argparse
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--ip', action='append', required=True)
    args = parser.parse_args()
    addresses = [ipaddress.ip_address(value) for value in args.ip]
    if any(not value.is_private for value in addresses):
        parser.error('Only local/private IP addresses are supported')
    tls = args.runtime / 'tls'
    key = serialization.load_pem_private_key((tls / 'localhost.key').read_bytes(), password=None)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local KonomiTV')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
            x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
            x509.IPAddress(ipaddress.ip_address('127.0.0.77')),
            *[x509.IPAddress(value) for value in addresses]]), critical=False)
        .sign(key, hashes.SHA256()))
    (tls / 'localhost.crt').write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    print('Updated local certificate IPs. Restart your KonomiTV instance to load it. No trust store changes.')


if __name__ == '__main__':
    main()
