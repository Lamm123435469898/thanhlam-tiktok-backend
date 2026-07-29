import os
import uuid
import secrets
import requests
from urllib.parse import urlencode
from flask import Flask, request, redirect, render_template, session
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

CLIENT_KEY = os.environ["TIKTOK_CLIENT_KEY"]
CLIENT_SECRET = os.environ["TIKTOK_CLIENT_SECRET"]
REDIRECT_URI = os.environ["TIKTOK_REDIRECT_URI"]

UPLOAD_FOLDER = "uploads"
ALLOWED_EXT = {"mp4"}
ALLOWED_MIME = {"video/mp4"}
MAX_FILE_SIZE_MB = 50
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename, mimetype):
    ext_ok = "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT
    mime_ok = mimetype in ALLOWED_MIME
    return ext_ok and mime_ok


def get_authorize_url():
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {
        "client_key": CLIENT_KEY,
        "scope": "user.info.basic,video.publish",  # chỉ liệt kê đúng 2 scope bạn muốn dùng
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)

def exchange_code_for_token(code):
    try:
        resp = requests.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            data={
                "client_key": CLIENT_KEY,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return resp.json()
    except requests.RequestException as e:
        return {"error": "network_error", "detail": str(e)}


def get_user_info(access_token):
    try:
        resp = requests.get(
            "https://open.tiktokapis.com/v2/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "open_id,display_name,avatar_url"},
            timeout=10,
        )
        return resp.json().get("data", {}).get("user", {})
    except requests.RequestException:
        return {}


def query_creator_info(access_token):
    """Bước bắt buộc theo tài liệu Content Posting API trước khi publish."""
    try:
        resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return resp.json().get("data", {})
    except requests.RequestException as e:
        return {"error": str(e)}


def post_tiktok_video(access_token, video_path, caption):
    file_size = os.path.getsize(video_path)

    # Bước 0: query creator info (bắt buộc theo docs, dùng để log/hiển thị)
    creator_info = query_creator_info(access_token)

    try:
        init = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "post_info": {"title": caption, "privacy_level": "SELF_ONLY"},
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": file_size,
                    "total_chunk_count": 1,
                },
            },
            timeout=15,
        )
        init_data = init.json()
    except requests.RequestException as e:
        return {"error": "init_network_error", "detail": str(e)}

    if "data" not in init_data or "upload_url" not in init_data.get("data", {}):
        return {"error": "init_failed", "detail": init_data, "creator_info": creator_info}

    upload_url = init_data["data"]["upload_url"]
    try:
        with open(video_path, "rb") as f:
            video_data = f.read()
        put_resp = requests.put(
            upload_url,
            data=video_data,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{file_size-1}/{file_size}",
            },
            timeout=60,
        )
        upload_ok = put_resp.status_code in (200, 201)
    except requests.RequestException as e:
        return {"error": "upload_network_error", "detail": str(e)}

    return {
        "success": upload_ok,
        "creator_info": creator_info,
        "publish_id": init_data.get("data", {}).get("publish_id"),
        "upload_status": put_resp.status_code,
    }


@app.route("/")
def index():
    user_info = None
    if "access_token" in session:
        user_info = get_user_info(session["access_token"])
    return render_template("index.html", user_info=user_info)


@app.route("/login")
def login():
    return redirect(get_authorize_url())


@app.route("/callback")
def callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "Invalid state — phiên có thể bị CSRF hoặc đã hết hạn.", 400

    code = request.args.get("code")
    if not code:
        return "Không nhận được code từ TikTok.", 400

    token_data = exchange_code_for_token(code)
    if "access_token" not in token_data:
        return f"Đổi token thất bại: {token_data}", 400

    session["access_token"] = token_data["access_token"]
    return redirect("/")


@app.route("/upload", methods=["POST"])
def upload():
    if "access_token" not in session:
        return redirect("/login")

    video_file = request.files.get("video")
    caption = request.form.get("caption", "").strip()
    user_info = get_user_info(session["access_token"])

    if not video_file or video_file.filename == "":
        return render_template("index.html", error="Vui lòng chọn file video.", user_info=user_info)

    if not allowed_file(video_file.filename, video_file.mimetype):
        return render_template("index.html", error="Chỉ chấp nhận file .mp4 hợp lệ.", user_info=user_info)

    safe_filename = f"{uuid.uuid4()}.mp4"
    video_path = os.path.join(UPLOAD_FOLDER, safe_filename)
    video_file.save(video_path)

    if os.path.getsize(video_path) > MAX_FILE_SIZE_MB * 1024 * 1024:
        os.remove(video_path)
        return render_template("index.html", error=f"File vượt quá {MAX_FILE_SIZE_MB}MB.", user_info=user_info)

    result = post_tiktok_video(session["access_token"], video_path, caption)

    if os.path.exists(video_path):
        os.remove(video_path)

    return render_template("index.html", result=result, user_info=user_info)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")
@app.route("/tiktok0SD9i62QN62ISBamXiRCdX1u3oN4i2Hh")
def tiktok_verify():
    return "tiktok-developers-site-verification=0SD9i62QN62ISBamXiRCdX1u3oN4i2Hh", 200, {"Content-Type": "text/plain"}

if __name__ == "__main__":
    # Đổi debug=False khi quay demo hoặc triển khai thật
    app.run(debug=False, port=5000)