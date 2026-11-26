"""Fail builds/runtimes that retain the compromised emSign trust anchor."""

import importlib.metadata
import json
import ssl

import certifi
from pip._vendor import certifi as pip_certifi


def check_bundle(label, context):
    certificates = context.get_ca_certs()
    if not certificates:
        raise RuntimeError(f"{label}: empty certificate store")
    for certificate in certificates:
        for rdn in certificate["subject"]:
            for key, value in rdn:
                if key == "commonName" and value == "emSign ECC Root CA - G3":
                    raise RuntimeError(f"{label}: compromised emSign root remains trusted")
    return len(certificates)


def main():
    versions = {}
    for package, minimum in (("certifi", (2026, 10, 19)), ("pip", (26, 2, 2))):
        version = importlib.metadata.version(package)
        if tuple(int(part) for part in version.split(".")) < minimum:
            raise RuntimeError(f"{package}: unsafe version {version}")
        versions[package] = version
    stores = {
        "certifi": ssl.create_default_context(cafile=certifi.where()),
        "pip_vendored": ssl.create_default_context(cafile=pip_certifi.where()),
        "system": ssl.create_default_context(),
    }
    counts = {label: check_bundle(label, context) for label, context in stores.items()}
    print(json.dumps({"versions": versions, "safe_store_counts": counts}))


if __name__ == "__main__":
    main()
