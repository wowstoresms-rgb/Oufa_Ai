import os, uuid, mimetypes, subprocess, shutil, asyncio, importlib.util
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from werkzeug.utils import secure_filename
from PIL import Image
import img2pdf
import requests
from pypdf import PdfReader
import edge_tts

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "outputs"
SKILLS = BASE / "skills"
for p in (UPLOADS, OUTPUTS, SKILLS):
    p.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", 500 * 1024 * 1024))
ALLOWED = {".png",".jpg",".jpeg",".webp",".bmp",".gif",".mp4",".mov",".mkv",".webm",
           ".mp3",".wav",".m4a",".aac",".pdf",".doc",".docx",".ppt",".pptx",".odt",".odp",".txt"}

def run_cmd(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=240)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "فشل تنفيذ العملية")[-1800:])
    return p

def save_uploads():
    saved = []
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED:
            continue
        name = f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
        path = UPLOADS / name
        f.save(path)
        saved.append(path)
    return saved

def output_url(path):
    return "/files/" + path.name

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/health")
def health():
    return jsonify(ok=True, service="Oufa_Ai")

@app.get("/files/<path:filename>")
def get_file(filename):
    # Only generated/uploaded files inside the controlled output directory are exposed.
    return send_from_directory(OUTPUTS, filename, as_attachment=False)

@app.post("/api/process")
def process():
    files = save_uploads()
    action = request.form.get("action", "assistant")
    prompt = request.form.get("prompt", "").strip()
    api_key = request.form.get("api_key", "").strip()
    try:
        if action == "image_to_pdf":
            imgs = [p for p in files if p.suffix.lower() in {".png",".jpg",".jpeg",".webp",".bmp"}]
            if not imgs: raise ValueError("أرفق صورة واحدة على الأقل.")
            out = OUTPUTS / f"{uuid.uuid4().hex}.pdf"
            # img2pdf handles common JPEG/PNG; normalize other formats with Pillow.
            normalized = []
            for p in imgs:
                if p.suffix.lower() in {".jpg",".jpeg",".png"}:
                    normalized.append(str(p))
                else:
                    q = UPLOADS / f"{uuid.uuid4().hex}.jpg"
                    Image.open(p).convert("RGB").save(q, quality=92)
                    normalized.append(str(q))
            out.write_bytes(img2pdf.convert(normalized))
            return jsonify(ok=True, url=output_url(out), filename=out.name, kind="file", text="تم تحويل الصور إلى PDF.")

        if action == "image_convert":
            imgs = [p for p in files if p.suffix.lower() in {".png",".jpg",".jpeg",".webp",".bmp",".gif"}]
            if not imgs: raise ValueError("أرفق صورة للتحويل.")
            fmt = request.form.get("format", "webp").upper()
            fmt = fmt if fmt in {"PNG","JPEG","WEBP"} else "WEBP"
            results=[]
            for p in imgs:
                out = OUTPUTS / f"{p.stem}_{uuid.uuid4().hex[:6]}.{fmt.lower()}"
                im = Image.open(p)
                if fmt == "JPEG": im = im.convert("RGB")
                im.save(out, format=fmt, quality=90)
                results.append({"url":output_url(out),"filename":out.name})
            return jsonify(ok=True, kind="files", files=results, text=f"تم تحويل {len(results)} صورة إلى {fmt}.")

        if action == "office_pdf":
            docs = [p for p in files if p.suffix.lower() in {".doc",".docx",".ppt",".pptx",".odt",".odp"}]
            if not docs: raise ValueError("أرفق ملف Word أو PowerPoint.")
            made=[]
            for p in docs:
                run_cmd(["libreoffice","--headless","--convert-to","pdf","--outdir",str(OUTPUTS),str(p)])
                dest=OUTPUTS/(p.stem+".pdf")
                if dest.exists(): made.append({"url":output_url(dest),"filename":dest.name})
            return jsonify(ok=True,kind="files",files=made,text="اكتمل تحويل ملفات Office إلى PDF.")

        if action == "pdf_summary":
            pdfs=[p for p in files if p.suffix.lower()==".pdf"]
            if not pdfs: raise ValueError("أرفق ملف PDF.")
            text_content="\n\n".join("\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages) for p in pdfs)
            return gemini(prompt or "لخص المستند في نقاط واضحة، واستخرج أهم المعلومات.", text_content, api_key)

        if action == "assistant":
            if not api_key: raise ValueError("أدخل مفتاح Gemini من زر حسابات AI لاستخدام الذكاء الاصطناعي.")
            extracted = prompt
            for p in files:
                if p.suffix.lower()==".pdf":
                    extracted += "\n\n" + "\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages)
                elif p.suffix.lower()==".txt":
                    extracted += "\n\n" + p.read_text(errors="ignore")
            return gemini(prompt or "حلل المحتوى وقدّم أفضل مساعدة ممكنة.", extracted, api_key)

        if action == "image_gen":
            if not prompt: raise ValueError("اكتب وصف الصورة أولاً.")
            # Pollinations public endpoint; no API key. Provider availability/limits may change.
            url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt)
            r = requests.get(url, params={"width":1024,"height":1024,"nologo":"true"}, timeout=180)
            r.raise_for_status()
            out=OUTPUTS/f"{uuid.uuid4().hex}.jpg"
            out.write_bytes(r.content)
            return jsonify(ok=True,url=output_url(out),filename=out.name,kind="image",text="تم إنشاء الصورة.")

        if action == "tts":
            if not prompt: raise ValueError("اكتب النص المراد تحويله إلى صوت.")
            voice=request.form.get("voice","ar-EG-SalmaNeural")
            if voice not in {"ar-EG-SalmaNeural","ar-EG-ShakirNeural","en-US-JennyNeural","en-US-GuyNeural"}:
                voice="ar-EG-SalmaNeural"
            out=OUTPUTS/f"{uuid.uuid4().hex}.mp3"
            async def make_audio():
                communicate=edge_tts.Communicate(prompt, voice)
                await communicate.save(str(out))
            asyncio.run(make_audio())
            return jsonify(ok=True,url=output_url(out),filename=out.name,kind="audio",text="تم إنشاء الملف الصوتي.")

        if action in {"video_merge","video_reel","video_compress"}:
            vids=[p for p in files if p.suffix.lower() in {".mp4",".mov",".mkv",".webm"}]
            if action=="video_reel":
                imgs=[p for p in files if p.suffix.lower() in {".png",".jpg",".jpeg",".webp"}]
                if not imgs: raise ValueError("أرفق صوراً لتحويلها إلى فيديو.")
                out=OUTPUTS/f"{uuid.uuid4().hex}.mp4"
                # Create a vertical slideshow with a gentle zoom/pan per image.
                listfile=UPLOADS/f"{uuid.uuid4().hex}.txt"
                lines=[]
                for p in imgs:
                    lines += [f"file '{p.as_posix()}'", "duration 3"]
                lines.append(f"file '{imgs[-1].as_posix()}'")
                listfile.write_text("\n".join(lines))
                run_cmd(["ffmpeg","-y","-f","concat","-safe","0","-i",str(listfile),"-vf",
                         "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                         "-r","30","-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart",str(out)])
            else:
                if not vids: raise ValueError("أرفق فيديو واحداً على الأقل.")
                out=OUTPUTS/f"{uuid.uuid4().hex}.mp4"
                if action=="video_compress":
                    run_cmd(["ffmpeg","-y","-i",str(vids[0]),"-vcodec","libx264","-crf","28","-preset","veryfast","-acodec","aac",str(out)])
                elif len(vids)==1:
                    shutil.copy2(vids[0],out)
                else:
                    listfile=UPLOADS/f"{uuid.uuid4().hex}.txt"
                    listfile.write_text("\n".join(f"file '{p.as_posix()}'" for p in vids))
                    run_cmd(["ffmpeg","-y","-f","concat","-safe","0","-i",str(listfile),"-c","copy",str(out)])
            return jsonify(ok=True,url=output_url(out),filename=out.name,kind="video",text="تم تجهيز الفيديو.")

        raise ValueError("نوع العملية غير مدعوم.")
    except Exception as e:
        return jsonify(ok=False,error=str(e)),400

def gemini(instruction, content, api_key):
    if not api_key:
        raise ValueError("مفتاح Gemini مطلوب لهذه المهمة.")
    try:
        from google import genai
        client=genai.Client(api_key=api_key)
        response=client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"التعليمات:\n{instruction}\n\nالمحتوى:\n{content[:900000]}"
        )
        return jsonify(ok=True,kind="text",text=response.text or "لم يتم إرجاع نص.")
    except Exception as e:
        raise RuntimeError(f"تعذر الاتصال بـ Gemini: {e}")

@app.get("/api/skills")
def list_skills():
    return jsonify(skills=[p.stem for p in SKILLS.glob("*.py") if not p.name.startswith("_")])

@app.post("/api/skills/<name>")
def run_skill(name):
    if not name.replace("_","").isalnum(): abort(400)
    path=SKILLS/f"{name}.py"
    if not path.exists(): abort(404)
    try:
        spec=importlib.util.spec_from_file_location(f"skill_{name}",path)
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        result=mod.run(request.get_json(silent=True) or {})
        return jsonify(ok=True,result=result)
    except Exception as e:
        return jsonify(ok=False,error=str(e)),400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")), debug=False)
