"""Build step: download the pinned bws CLI for this CPU and verify Bitwarden's published checksum.
A separate file (not a RUN heredoc) because the HP's Docker has no BuildKit, and the legacy
builder silently skips heredoc bodies."""
import hashlib, io, os, platform, urllib.request, zipfile
arch = os.environ.get("TARGETARCH") or {"aarch64": "arm64", "x86_64": "amd64"}[platform.machine()]
triple, want = {
    "arm64": ("aarch64-unknown-linux-gnu", os.environ["BWS_SHA256_ARM64"]),
    "amd64": ("x86_64-unknown-linux-gnu", os.environ["BWS_SHA256_AMD64"]),
}[arch]
v = os.environ["BWS_VERSION"]
url = f"https://github.com/bitwarden/sdk-sm/releases/download/bws-v{v}/bws-{triple}-{v}.zip"
data = urllib.request.urlopen(url, timeout=120).read()
got = hashlib.sha256(data).hexdigest()
assert got == want, f"bws checksum mismatch for {arch}: {got}"
zipfile.ZipFile(io.BytesIO(data)).extract("bws", "/usr/local/bin")
os.chmod("/usr/local/bin/bws", 0o755)
