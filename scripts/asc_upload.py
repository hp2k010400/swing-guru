"""Upload an EAS iOS build straight to App Store Connect (TestFlight), skipping the EAS submit queue.

    python scripts/asc_upload.py <eas-build-id>

Waits for the EAS build, downloads the .ipa, checks its bundle id / version / build number, uploads it
with the App Store Connect Build Uploads API and waits until Apple marks the build VALID. TestFlight
only: it never creates an App Store version or submits anything for review.

Needs the App Store Connect API key (.p8) on this machine. Configure with environment variables
ASC_KEY_ID, ASC_ISSUER_ID and ASC_KEY_PATH (defaults: the Swing Guru team key in Downloads). The key
is read from disk and never printed.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
# The App Store Connect app is looked up from the .ipa's bundle id, so the same script uploads the
# release app (com.swingguru.app) and the separate staging app (com.swingguru.app.staging).
RELEASE_BUNDLE_ID = "com.swingguru.app"
KEY_ID = os.environ["ASC_KEY_ID"]
ISSUER = os.environ["ASC_ISSUER_ID"]
KEY_PATH = Path(os.environ.get("ASC_KEY_PATH", Path.home() / "Downloads" / f"AuthKey_{KEY_ID}.p8"))


def asc() -> httpx.Client:
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "iat": now, "exp": now + 1100, "aud": "appstoreconnect-v1"},
                       KEY_PATH.read_text(), algorithm="ES256", headers={"kid": KEY_ID, "typ": "JWT"})
    return httpx.Client(base_url="https://api.appstoreconnect.apple.com", timeout=120,
                        headers={"Authorization": f"Bearer {token}"})


def eas_build(build_id: str) -> dict:
    out = subprocess.run(f"npx eas-cli build:view {build_id} --json", cwd=ROOT / "mobile", shell=True,
                         capture_output=True, text=True)
    return json.loads(out.stdout)


def main(build_id: str) -> int:
    while True:
        build = eas_build(build_id)
        if build["status"] in ("FINISHED", "ERRORED", "CANCELED"):
            break
        print(time.strftime("%H:%M:%S"), "EAS build:", build["status"], flush=True)
        time.sleep(60)
    if build["status"] != "FINISHED":
        print("EAS build did not finish:", build["status"], build.get("error"))
        return 1
    version, number = build["appVersion"], build["appBuildVersion"]
    ipa = ROOT / ".tmp" / f"build{number}" / f"SwingGuru-build{number}.ipa"
    ipa.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", build["artifacts"]["buildUrl"], follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with open(ipa, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    with zipfile.ZipFile(ipa) as z:
        name = next(n for n in z.namelist() if n.endswith(".app/Info.plist") and n.count("/") == 2)
        info = plistlib.loads(z.read(name))
    found = (info["CFBundleIdentifier"], info["CFBundleShortVersionString"], info["CFBundleVersion"])
    print("ipa:", found, ipa.stat().st_size, "bytes", flush=True)
    bundle_id = info["CFBundleIdentifier"]
    if bundle_id != build.get("appIdentifier", bundle_id) or found[1:] != (version, number):
        print("refusing: the .ipa does not match the EAS build")
        return 1
    if bundle_id not in (RELEASE_BUNDLE_ID, RELEASE_BUNDLE_ID + ".staging"):
        print(f"refusing: unexpected bundle id {bundle_id}")
        return 1

    c = asc()
    apps = c.get("/v1/apps", params={"filter[bundleId]": bundle_id}).json().get("data", [])
    if not apps:
        print(f"no App Store Connect app exists for {bundle_id} yet: create it first (see the staging guide)")
        return 1
    APP_ID = apps[0]["id"]
    print("App Store Connect app:", APP_ID, bundle_id, flush=True)
    upload = c.post("/v1/buildUploads", json={"data": {"type": "buildUploads", "attributes": {
        "cfBundleShortVersionString": version, "cfBundleVersion": number, "platform": "IOS"},
        "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}}}})
    if upload.status_code != 201:
        print("buildUploads refused:", upload.status_code, upload.text[:500])
        return 1
    upload_id = upload.json()["data"]["id"]
    file = c.post("/v1/buildUploadFiles", json={"data": {"type": "buildUploadFiles", "attributes": {
        "assetType": "ASSET", "fileName": ipa.name, "fileSize": ipa.stat().st_size, "uti": "com.apple.ipa"},
        "relationships": {"buildUpload": {"data": {"type": "buildUploads", "id": upload_id}}}}})
    if file.status_code != 201:
        print("buildUploadFiles refused:", file.status_code, file.text[:500])
        return 1
    data = file.json()["data"]
    content = ipa.read_bytes()
    for op in sorted(data["attributes"]["uploadOperations"], key=lambda o: o["offset"]):
        headers = {h["name"]: h["value"] for h in (op.get("requestHeaders") or [])}
        part = httpx.request(op["method"], op["url"], content=content[op["offset"]: op["offset"] + op["length"]],
                             headers=headers, timeout=300)
        if part.status_code >= 300:
            print("part upload failed:", part.status_code)
            return 1
    done = asc().patch(f"/v1/buildUploadFiles/{data['id']}", json={"data": {
        "type": "buildUploadFiles", "id": data["id"], "attributes": {"uploaded": True}}})
    print("uploaded:", done.status_code, done.json()["data"]["attributes"]["assetDeliveryState"]["state"], flush=True)

    for _ in range(90):
        c = asc()
        state = c.get(f"/v1/buildUploads/{upload_id}").json()["data"]["attributes"]["state"]
        builds = c.get("/v1/builds", params={"filter[app]": APP_ID, "filter[version]": number,
                                             "fields[builds]": "processingState"}).json().get("data", [])
        processing = builds[0]["attributes"]["processingState"] if builds else None
        print(time.strftime("%H:%M:%S"), "upload:", state["state"], state["errors"] or "", "| build:", processing, flush=True)
        if processing in ("VALID", "FAILED", "INVALID") or state["state"] == "FAILED":
            return 0 if processing == "VALID" else 1
        time.sleep(60)
    print("still processing after 90 minutes")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
