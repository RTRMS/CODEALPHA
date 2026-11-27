"""AnnSeva backend: FastAPI + SQLite + ML recommendation engine.

Production-minded fixes:
- token-based authentication and role authorization
- atomic approval/scheduling with slot quantity capacity
- timezone-aware scheduling
- accurate standing-offer GPS persistence
- explicit cold-start ML baseline and persisted Random Forest model
- training history recorded only when an actual completion quantity is supplied
- completion workflow and multiple-application-safe farmer views
"""
from pathlib import Path
import os, sys, sqlite3, uuid, math, secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
# Overridable so tests can point at a throwaway database instead of the real one.
DB_PATH = Path(os.getenv("ANNSEVA_DB_PATH", str(ROOT / "annseva.db")))
ML_DIR = ROOT / "ml"
if str(ML_DIR) not in sys.path:
    sys.path.insert(0, str(ML_DIR))
from recommendation_engine import recommend_farmers, allocate_procurement

app = FastAPI(title="AnnSeva Procurement API", version="4.0")
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("ANNSEVA_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5500,http://localhost:5500,http://127.0.0.1:5501,http://localhost:5501,http://127.0.0.1:5502,http://localhost:5502").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False,
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "X-API-Key"])

TZ = ZoneInfo(os.getenv("ANNSEVA_TIMEZONE", "Asia/Kolkata"))
ADMIN_PASSWORD = os.getenv("ANNSEVA_ADMIN_PASSWORD", "admin123")
FARMER_PASSWORD = os.getenv("ANNSEVA_FARMER_PASSWORD", "farmer123")
SESSION_TTL_HOURS = int(os.getenv("ANNSEVA_SESSION_TTL_HOURS", "24"))
SESSIONS = {}  # in-process cache; durable sessions are also stored in SQLite

class LoginIn(BaseModel):
    role: str
    farmer_id: Optional[str] = None
    admin_id: Optional[str] = None
    password: str

class ApplicationIn(BaseModel):
    farmer_id: str
    name: str
    crop: str
    quantity: float = Field(gt=0)
    location: str
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)

class RequirementIn(BaseModel):
    crop: str
    required_quantity: float = Field(gt=0)
    centre: str
    centre_capacity: float = Field(gt=0)
    slot_capacity: float = Field(default=50, gt=0)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)

class RecommendationIn(BaseModel):
    top_k: int = Field(default=10, ge=1, le=50)

class ApproveIn(BaseModel):
    application_id: str
    quantity: Optional[float] = Field(default=None, gt=0)

class ScheduleIn(BaseModel):
    application_id: str

class CompleteIn(BaseModel):
    application_id: str
    actual_quantity: float = Field(gt=0)

class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def db():
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _has_column(con, table, column):
    return any(r[1] == column for r in con.execute(f"PRAGMA table_info({table})"))


def init_db():
    with db() as con:
        req_sql_row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='requirements'").fetchone()
        if req_sql_row and "CHECK(id=1)" in (req_sql_row[0] or "").replace(" ", ""):
            con.execute("ALTER TABLE requirements RENAME TO requirements_legacy")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS farmers (
          farmer_id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
          verified INTEGER NOT NULL DEFAULT 0, latitude REAL, longitude REAL,
          priority_score REAL NOT NULL DEFAULT 0.5, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crop_offers (
          offer_id INTEGER PRIMARY KEY AUTOINCREMENT, farmer_id TEXT NOT NULL REFERENCES farmers(farmer_id) ON DELETE CASCADE,
          crop TEXT NOT NULL, quantity REAL NOT NULL CHECK(quantity > 0), location TEXT NOT NULL,
          latitude REAL, longitude REAL, updated_at TEXT NOT NULL, UNIQUE(farmer_id, crop)
        );
        CREATE TABLE IF NOT EXISTS requirements (
          id INTEGER PRIMARY KEY AUTOINCREMENT, crop TEXT NOT NULL, required_quantity REAL NOT NULL,
          centre TEXT NOT NULL, centre_capacity REAL NOT NULL, slot_capacity REAL NOT NULL DEFAULT 50,
          latitude REAL, longitude REAL, active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS applications (
          application_id TEXT PRIMARY KEY, farmer_id TEXT NOT NULL REFERENCES farmers(farmer_id),
          requirement_id INTEGER REFERENCES requirements(id), crop TEXT NOT NULL, quantity REAL NOT NULL CHECK(quantity > 0),
          status TEXT NOT NULL DEFAULT 'Submitted', allocation REAL NOT NULL DEFAULT 0,
          centre TEXT NOT NULL DEFAULT 'Awaiting allocation', schedule_date TEXT, schedule_slot TEXT, token TEXT,
          assigned_admin_id TEXT, reviewed_by_admin_id TEXT, reviewed_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(farmer_id, requirement_id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, farmer_id TEXT NOT NULL, title TEXT NOT NULL,
          message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
          admin_id TEXT PRIMARY KEY, name TEXT NOT NULL, jurisdiction TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_assignment_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT NOT NULL,
          admin_id TEXT NOT NULL, assigned_at TEXT NOT NULL, decision TEXT,
          decision_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_offer_crop ON crop_offers(crop);
        CREATE INDEX IF NOT EXISTS idx_app_req_status ON applications(requirement_id,status);
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, role TEXT NOT NULL, farmer_id TEXT, admin_id TEXT,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        CREATE TABLE IF NOT EXISTS model_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT, crop_match REAL NOT NULL,
          quantity_fit REAL NOT NULL, location_match REAL NOT NULL, verified REAL NOT NULL,
          priority_score REAL NOT NULL, outcome REAL NOT NULL, created_at TEXT NOT NULL
        );
        """)
        if not _has_column(con, "applications", "requirement_id"):
            con.execute("ALTER TABLE applications ADD COLUMN requirement_id INTEGER")
        for col, definition in (("assigned_admin_id", "TEXT"), ("reviewed_by_admin_id", "TEXT"), ("reviewed_at", "TEXT")):
            if not _has_column(con, "applications", col):
                con.execute(f"ALTER TABLE applications ADD COLUMN {col} {definition}")
        if not _has_column(con, "requirements", "slot_capacity"):
            con.execute("ALTER TABLE requirements ADD COLUMN slot_capacity REAL NOT NULL DEFAULT 50")
        for col in ("latitude", "longitude"):
            if not _has_column(con, "farmers", col): con.execute(f"ALTER TABLE farmers ADD COLUMN {col} REAL")
            if not _has_column(con, "crop_offers", col): con.execute(f"ALTER TABLE crop_offers ADD COLUMN {col} REAL")
        if not _has_column(con, "requirements", "active"):
            con.execute("ALTER TABLE requirements ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

        legacy_req = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='requirements_legacy'").fetchone()
        req_count = con.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
        now = local_now().isoformat(timespec="seconds")
        if req_count == 0 and legacy_req:
            old = con.execute("SELECT * FROM requirements_legacy LIMIT 1").fetchone()
            if old:
                con.execute("INSERT INTO requirements(crop,required_quantity,centre,centre_capacity,slot_capacity,active,created_at,updated_at) VALUES(?,?,?,?,50,1,?,?)",
                            (old["crop"], old["required_quantity"], old["centre"], old["centre_capacity"], now, now))
            con.execute("DROP TABLE requirements_legacy")
        if con.execute("SELECT COUNT(*) FROM requirements").fetchone()[0] == 0:
            con.execute("INSERT INTO requirements(crop,required_quantity,centre,centre_capacity,slot_capacity,active,created_at,updated_at) VALUES(?,?,?,?,50,1,?,?)",
                        ("Wheat", 300, "Kurnool Procurement Centre", 500, now, now))
        else:
            con.execute("UPDATE requirements SET active=0")
            last_id = con.execute("SELECT id FROM requirements ORDER BY id DESC LIMIT 1").fetchone()[0]
            con.execute("UPDATE requirements SET active=1 WHERE id=?", (last_id,))

        if con.execute("SELECT COUNT(*) FROM farmers").fetchone()[0] == 0:
            farmers=[("FMR-1042","Ravi Kumar","Kurnool",1,.90),("FMR-1175","Suresh Reddy","Kurnool",1,.82),
                     ("FMR-0921","Lakshmi Devi","Nandyal",1,.74),("FMR-1264","Mahesh","Adoni",1,.70)]
            con.executemany("INSERT INTO farmers(farmer_id,name,location,verified,priority_score,created_at) VALUES (?,?,?,?,?,?)",
                            [(a,b,c,d,e,now) for a,b,c,d,e in farmers])
        if con.execute("SELECT COUNT(*) FROM crop_offers").fetchone()[0] == 0:
            offers=[("FMR-1042","Wheat",120,"Kurnool"),("FMR-1175","Wheat",180,"Kurnool"),("FMR-0921","Wheat",90,"Nandyal"),("FMR-1264","Rice",150,"Adoni")]
            con.executemany("INSERT INTO crop_offers(farmer_id,crop,quantity,location,updated_at) VALUES(?,?,?,?,?)", [x+(now,) for x in offers])
        con.executemany(
            "INSERT OR IGNORE INTO admins(admin_id,name,jurisdiction,active,created_at) VALUES(?,?,?,?,?)",
            [("ADM-KURNOOL","Anil Rao","Kurnool",1,now),
             ("ADM-NANDYAL","Priya Reddy","Nandyal",1,now),
             ("ADM-ADONI","Vikram Singh","Adoni",1,now)]
        )
        req=con.execute("SELECT * FROM requirements WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
        sync_applications_for_requirement(con, req["id"] if req else None)
        if con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0:
            con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",
                        ("FMR-1042","Application recorded","Your standing crop offer is registered with AnnSeva.",now))


def _location_tokens(value):
    return set(" ".join((value or "").lower().replace(",", " ").split()).split())

def _is_local_jurisdiction(farmer_location, admin_jurisdiction):
    ft = _location_tokens(farmer_location)
    at = _location_tokens(admin_jurisdiction)
    if not ft or not at:
        return False
    # A shared village/town/jurisdiction token is treated as local.
    return bool(ft & at)

def assign_admin_for_application(con, application_id):
    """Assign one active, non-local admin at random and keep the assignment fixed."""
    app = con.execute("""
        SELECT a.application_id, a.assigned_admin_id, f.location AS farmer_location
        FROM applications a JOIN farmers f ON f.farmer_id=a.farmer_id
        WHERE a.application_id=?
    """, (application_id,)).fetchone()
    if not app:
        return None
    if app["assigned_admin_id"]:
        return con.execute("SELECT * FROM admins WHERE admin_id=? AND active=1",
                           (app["assigned_admin_id"],)).fetchone()

    admins = con.execute("SELECT * FROM admins WHERE active=1 ORDER BY admin_id").fetchall()
    eligible = [a for a in admins if not _is_local_jurisdiction(app["farmer_location"], a["jurisdiction"])]
    if not eligible:
        return None

    chosen = secrets.choice(eligible)
    now = local_now().isoformat(timespec="seconds")
    con.execute("UPDATE applications SET assigned_admin_id=?, updated_at=? WHERE application_id=?",
                (chosen["admin_id"], now, application_id))
    con.execute("""
        INSERT INTO admin_assignment_history(application_id,admin_id,assigned_at,decision,decision_at)
        VALUES(?,?,?,NULL,NULL)
    """, (application_id, chosen["admin_id"], now))
    return chosen

def sync_applications_for_requirement(con, requirement_id):
    if not requirement_id: return
    req=con.execute("SELECT * FROM requirements WHERE id=?",(requirement_id,)).fetchone()
    if not req: return
    offers=con.execute("SELECT * FROM crop_offers WHERE lower(crop)=lower(?) AND quantity>0",(req["crop"],)).fetchall()
    now=local_now().isoformat(timespec="seconds")
    for offer in offers:
        exists=con.execute("SELECT application_id,status FROM applications WHERE farmer_id=? AND requirement_id=?",(offer["farmer_id"],requirement_id)).fetchone()
        if exists:
            if exists["status"]=="Submitted":
                con.execute("UPDATE applications SET quantity=?,crop=?,updated_at=? WHERE application_id=?",(offer["quantity"],offer["crop"],now,exists["application_id"]))
            assign_admin_for_application(con, exists["application_id"])
        else:
            app_id="APP-"+uuid.uuid4().hex[:8].upper()
            con.execute("INSERT INTO applications(application_id,farmer_id,requirement_id,crop,quantity,status,allocation,centre,created_at,updated_at) VALUES(?,?,?,?,?,'Submitted',0,'Awaiting allocation',?,?)",
                        (app_id,offer["farmer_id"],requirement_id,offer["crop"],offer["quantity"],now,now))
            assign_admin_for_application(con, app_id)


# Time helpers are defined before database initialization because initial demo
# applications may receive an admin assignment during startup.
def local_now(): return datetime.now(TZ)
def iso_now(): return local_now().isoformat(timespec="seconds")


# Initialize the SQLite schema and demo data whenever the server starts.
# This is intentionally placed after the helper functions used by init_db().
init_db()

def current_requirement(con):
    r=con.execute("SELECT * FROM requirements WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if not r: raise HTTPException(500,"Requirement is not configured")
    allocated=con.execute("SELECT COALESCE(SUM(allocation),0) FROM applications WHERE requirement_id=? AND status IN ('Approved','Scheduled','Completed')",(r["id"],)).fetchone()[0]
    return {"id":r["id"],"crop":r["crop"],"needed":r["required_quantity"],"centre":r["centre"],"capacity":r["centre_capacity"],
            "slot_capacity":r["slot_capacity"],"latitude":r["latitude"],"longitude":r["longitude"],"allocated":round(float(allocated),2),
            "remaining":round(max(0,float(r["required_quantity"])-float(allocated)),2)}


def snapshot(farmer_id="FMR-1042", role="admin"):
    with db() as con:
        req=current_requirement(con)
        app_rows = con.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall() if role=="admin" else con.execute("SELECT * FROM applications WHERE farmer_id=? ORDER BY created_at DESC",(farmer_id,)).fetchall()
        apps=[dict(x) for x in app_rows]
        farmers=[]
        farmer_rows = con.execute("SELECT * FROM farmers ORDER BY created_at").fetchall() if role=="admin" else con.execute("SELECT * FROM farmers WHERE farmer_id=?",(farmer_id,)).fetchall()
        for f in farmer_rows:
            a=con.execute("SELECT * FROM applications WHERE farmer_id=? AND requirement_id=? ORDER BY created_at DESC LIMIT 1",(f["farmer_id"],req["id"])).fetchone()
            offer=con.execute("SELECT * FROM crop_offers WHERE farmer_id=? ORDER BY updated_at DESC LIMIT 1",(f["farmer_id"],)).fetchone()
            farmers.append({"id":f["farmer_id"],"name":f["name"],"crop":a["crop"] if a else (offer["crop"] if offer else "—"),
                            "qty":a["quantity"] if a else (offer["quantity"] if offer else 0),"location":f["location"],"verified":bool(f["verified"]),
                            "score":round(f["priority_score"]*100),"status":a["status"] if a else "Registered","applicationId":a["application_id"] if a else None,
                            "approvedQty":a["allocation"] if a else 0,"scheduled":bool(a and a["schedule_date"])})
        notifications=[dict(x) for x in con.execute("SELECT * FROM notifications WHERE farmer_id=? ORDER BY id DESC",(farmer_id,))]
        admins=[dict(x) for x in con.execute("SELECT admin_id,name,jurisdiction,active FROM admins WHERE active=1 ORDER BY admin_id")]
        return {"requirement":req,"farmers":farmers,"applications":apps,"notifications":notifications,"admins":admins,"farmer_id":farmer_id}


def auth(authorization: Optional[str]=Header(default=None), x_api_key: Optional[str]=Header(default=None)):
    expected=os.getenv("ANNSEVA_API_KEY")
    if expected and x_api_key==expected: return {"role":"admin","farmer_id":None}
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401,"Authentication required")
    token=authorization.split(" ",1)[1].strip()
    if not token: raise HTTPException(401,"Authentication required")

    # Sessions are persisted in SQLite so restarting server.py does not
    # invalidate every logged-in browser. Keep the in-memory cache only as a
    # fast path. Expired sessions are removed lazily.
    session=SESSIONS.get(token)
    now=datetime.now(timezone.utc)
    if session:
        try:
            if datetime.fromisoformat(session["expires_at"]) > now:
                return session
        except Exception:
            pass
        SESSIONS.pop(token, None)

    with db() as con:
        row=con.execute("SELECT token,role,farmer_id,admin_id,expires_at FROM sessions WHERE token=?",(token,)).fetchone()
        if not row:
            raise HTTPException(401,"Session expired or invalid")
        try:
            expires=datetime.fromisoformat(row["expires_at"])
        except Exception:
            expires=now-timedelta(seconds=1)
        if expires <= now:
            con.execute("DELETE FROM sessions WHERE token=?",(token,))
            raise HTTPException(401,"Session expired or invalid")
        session={"role":row["role"],"farmer_id":row["farmer_id"],"admin_id":row["admin_id"],"expires_at":row["expires_at"]}
        SESSIONS[token]=session
        return session

def require_admin(user=Depends(auth)):
    if user["role"]!="admin": raise HTTPException(403,"Admin access required")
    return user

def require_farmer(user=Depends(auth)):
    if user["role"]!="farmer": raise HTTPException(403,"Farmer access required")
    return user

@app.post("/api/login")
def login(payload: LoginIn):
    role=payload.role.strip().lower()
    if role not in ("admin","farmer"): raise HTTPException(400,"Role must be admin or farmer")
    if role=="admin":
        if not payload.admin_id: raise HTTPException(400,"Admin ID is required")
        with db() as con:
            admin=con.execute("SELECT admin_id FROM admins WHERE admin_id=? AND active=1",(payload.admin_id,)).fetchone()
        if not admin or not secrets.compare_digest(payload.password,ADMIN_PASSWORD):
            raise HTTPException(401,"Invalid admin credentials")
        farmer_id=None
    else:
        if not payload.farmer_id: raise HTTPException(400,"Farmer ID is required")
        with db() as con:
            f=con.execute("SELECT farmer_id FROM farmers WHERE farmer_id=?",(payload.farmer_id,)).fetchone()
        if not f or not secrets.compare_digest(payload.password,FARMER_PASSWORD): raise HTTPException(401,"Invalid farmer credentials")
        farmer_id=payload.farmer_id
    token=secrets.token_urlsafe(32)
    now=datetime.now(timezone.utc)
    expires=now+timedelta(hours=SESSION_TTL_HOURS)
    session={"role":role,"farmer_id":farmer_id,"admin_id":payload.admin_id if role=="admin" else None,"expires_at":expires.isoformat()}
    SESSIONS[token]=session
    with db() as con:
        # Opportunistically clean old sessions while creating the new one.
        con.execute("DELETE FROM sessions WHERE expires_at<=?",(now.isoformat(),))
        con.execute("INSERT INTO sessions(token,role,farmer_id,admin_id,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                    (token,session["role"],session["farmer_id"],session["admin_id"],now.isoformat(),expires.isoformat()))
    return {"token":token,"role":role,"farmer_id":farmer_id,"admin_id":payload.admin_id if role=="admin" else None}

@app.post("/api/logout")
def logout(authorization: Optional[str]=Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        token=authorization.split(" ",1)[1].strip()
        SESSIONS.pop(token,None)
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token=?",(token,))
    return {"ok":True}

@app.get("/api/health")
def health(): return {"status":"ok","database":DB_PATH.name,"service":"AnnSeva"}

@app.get("/api/state")
def get_state(user=Depends(auth)):
    return snapshot(user["farmer_id"] or "FMR-1042", user["role"])

@app.post("/api/applications")
def create_application(payload: ApplicationIn, user=Depends(require_farmer)):
    if payload.farmer_id!=user["farmer_id"]: raise HTTPException(403,"You may only edit your own farmer record")
    now=iso_now()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO farmers(farmer_id,name,location,verified,priority_score,created_at) VALUES(?,?,?,0,COALESCE((SELECT priority_score FROM farmers WHERE farmer_id=?),0.85),?) ON CONFLICT(farmer_id) DO UPDATE SET name=excluded.name,location=excluded.location",
                    (payload.farmer_id,payload.name,payload.location,payload.farmer_id,now))
        con.execute("UPDATE farmers SET latitude=?,longitude=? WHERE farmer_id=?",(payload.latitude,payload.longitude,payload.farmer_id))
        con.execute("INSERT INTO crop_offers(farmer_id,crop,quantity,location,latitude,longitude,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(farmer_id,crop) DO UPDATE SET quantity=excluded.quantity,location=excluded.location,latitude=excluded.latitude,longitude=excluded.longitude,updated_at=excluded.updated_at",
                    (payload.farmer_id,payload.crop,payload.quantity,payload.location,payload.latitude,payload.longitude,now))
        req=current_requirement(con)
        crop_matches = payload.crop.strip().lower() == req["crop"].strip().lower()
        if crop_matches:
            existing=con.execute("SELECT application_id,status FROM applications WHERE farmer_id=? AND requirement_id=?",(payload.farmer_id,req["id"])).fetchone()
            if existing:
                app_id=existing["application_id"]
                if existing["status"]=="Submitted":
                    con.execute("UPDATE applications SET quantity=?,crop=?,updated_at=? WHERE application_id=?",(payload.quantity,payload.crop,now,app_id))
                elif existing["status"] in ("Scheduled","Completed"):
                    raise HTTPException(409,"This requirement has already been processed for this farmer")
            else:
                app_id="APP-"+uuid.uuid4().hex[:8].upper()
                con.execute("INSERT INTO applications(application_id,farmer_id,requirement_id,crop,quantity,status,allocation,centre,created_at,updated_at) VALUES(?,?,?,?,?,'Submitted',0,'Awaiting allocation',?,?)",(app_id,payload.farmer_id,req["id"],payload.crop,payload.quantity,now,now))
            assign_admin_for_application(con, app_id)
            con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",(payload.farmer_id,"Crop application updated",f"{payload.crop} ({payload.quantity:g} qtl) is registered for the active requirement.",now))
        else:
            # Keep every farmer submission visible in My Applications even when the
            # active requirement is for another crop. It is a standing offer and
            # becomes eligible automatically when a matching requirement is created.
            existing=con.execute("SELECT application_id,status FROM applications WHERE farmer_id=? AND requirement_id IS NULL AND lower(crop)=lower(?) ORDER BY created_at DESC LIMIT 1",(payload.farmer_id,payload.crop)).fetchone()
            if existing and existing["status"] in ("Submitted","Registered"):
                con.execute("UPDATE applications SET quantity=?,updated_at=? WHERE application_id=?",(payload.quantity,now,existing["application_id"]))
            else:
                app_id="APP-"+uuid.uuid4().hex[:8].upper()
                con.execute("INSERT INTO applications(application_id,farmer_id,requirement_id,crop,quantity,status,allocation,centre,created_at,updated_at) VALUES(?,?,?,?,?,'Registered',0,'Awaiting matching',?,?)",(app_id,payload.farmer_id,None,payload.crop,payload.quantity,now,now))
            con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",(payload.farmer_id,"Crop offer saved",f"Your {payload.crop} offer ({payload.quantity:g} qtl) is visible in Applications and will be matched when a {payload.crop} requirement is created.",now))
    return snapshot(user["farmer_id"], "farmer")

@app.post("/api/requirements")
def set_requirement(payload: RequirementIn, user=Depends(require_admin)):
    now=iso_now()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        cur=con.execute("INSERT INTO requirements(crop,required_quantity,centre,centre_capacity,slot_capacity,latitude,longitude,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (payload.crop,payload.required_quantity,payload.centre,payload.centre_capacity,payload.slot_capacity,payload.latitude,payload.longitude,now,now))
        req_id=cur.lastrowid; sync_applications_for_requirement(con,req_id)
        for row in con.execute("SELECT farmer_id FROM crop_offers WHERE lower(crop)=lower(?)",(payload.crop,)).fetchall():
            con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",(row["farmer_id"],"New procurement requirement",f"A new {payload.crop} requirement is available. Your standing offer was matched automatically.",now))
    return snapshot("FMR-1042", "admin")


def haversine_km(lat1,lon1,lat2,lon2):
    R=6371.0; p1,p2=math.radians(lat1),math.radians(lat2); dp=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(max(0,min(1,a))))

def normalize_location(s):
    return " ".join((s or "").lower().replace(","," ").split())

def calculate_location_score(farmer_lat,farmer_lon,centre_lat,centre_lon,farmer_location,centre):
    if None not in (farmer_lat,farmer_lon,centre_lat,centre_lon):
        d=haversine_km(farmer_lat,farmer_lon,centre_lat,centre_lon)
        return max(0.0,1.0-min(d/100.0,1.0))
    fl=normalize_location(farmer_location); cl=normalize_location(centre)
    if not fl or not cl: return 0.0
    ft=set(fl.split()); ct=set(cl.split()); overlap=len(ft & ct)
    return min(1.0, overlap/max(1,min(len(ft),len(ct))))

@app.get("/api/admins")
def list_admins(user=Depends(require_admin)):
    with db() as con:
        return {"admins":[dict(x) for x in con.execute("SELECT admin_id,name,jurisdiction,active FROM admins WHERE active=1 ORDER BY admin_id")]}

@app.post("/api/recommendations")
def recommendations(payload: RecommendationIn, user=Depends(require_admin)):
    with db() as con:
        r=current_requirement(con); sync_applications_for_requirement(con,r["id"])
        rows=con.execute("SELECT a.application_id,a.farmer_id,a.assigned_admin_id,f.name,a.crop,a.quantity,f.location,f.latitude,f.longitude,f.verified,f.priority_score FROM applications a JOIN farmers f ON f.farmer_id=a.farmer_id WHERE a.requirement_id=? AND a.status='Submitted' AND a.quantity>0",(r["id"],)).fetchall()
        for x in rows:
            if not x["assigned_admin_id"]:
                assign_admin_for_application(con, x["application_id"])
        rows=con.execute("SELECT a.application_id,a.farmer_id,a.assigned_admin_id,f.name,a.crop,a.quantity,f.location,f.latitude,f.longitude,f.verified,f.priority_score FROM applications a JOIN farmers f ON f.farmer_id=a.farmer_id WHERE a.requirement_id=? AND a.status='Submitted' AND a.quantity>0",(r["id"],)).fetchall()
        farmers=[]
        for x in rows:
            farmers.append({"farmer_id":x["farmer_id"],"name":x["name"],"crop":x["crop"],"quantity":x["quantity"],"location":x["location"],
                            "location_score":calculate_location_score(x["latitude"],x["longitude"],r["latitude"],r["longitude"],x["location"],r["centre"]),
                            "verified":bool(x["verified"]),"priority_score":x["priority_score"],"application_id":x["application_id"],"assigned_admin_id":x["assigned_admin_id"]})
        remaining=max(0,r["remaining"]); ranked, model_type = recommend_farmers(farmers,{"crop":r["crop"],"remaining_quantity":remaining},payload.top_k, return_meta=True)
        if ranked.empty: return {"recommendations":[],"allocations":[],"model":"none","message":"No eligible verified farmers found for this crop."}
        recs=ranked.to_dict(orient="records")
        for rec in recs:
            src=next(f for f in farmers if f["farmer_id"]==rec["farmer_id"]); rec["application_id"]=src["application_id"]; rec["assigned_admin_id"]=src.get("assigned_admin_id")
        alloc=allocate_procurement(ranked,remaining,max(0,r["capacity"]-r["allocated"]),0)
        allocations=alloc.to_dict(orient="records") if not alloc.empty else []
        for a in allocations:
            src=next(f for f in farmers if f["farmer_id"]==a["farmer_id"]); a["application_id"]=src["application_id"]; a["centre"]=r["centre"]
        return {"recommendations":recs,"allocations":allocations,"model":model_type,
                "message":"Random Forest recommendations generated." if model_type=="random_forest" else "Cold-start baseline used; insufficient real completion history for Random Forest."}


def assign_schedule(con,application_id,centre,slot_capacity):
    now=local_now().replace(second=0,microsecond=0); first=now.replace(hour=10,minute=30)
    if first<=now: first += timedelta(days=1)
    for day_offset in range(0,30):
        day=first+timedelta(days=day_offset)
        for slot_index in range(14):
            slot_dt=day+timedelta(minutes=30*slot_index)
            date_display=slot_dt.strftime("%d %b %Y"); slot_display=slot_dt.strftime("%I:%M %p")
            used=con.execute("SELECT COALESCE(SUM(allocation),0) FROM applications WHERE schedule_date=? AND schedule_slot=? AND centre=? AND application_id<>? AND status IN ('Scheduled','Completed')",
                             (date_display,slot_display,centre,application_id)).fetchone()[0]
            if float(used)<float(slot_capacity): return date_display,slot_display,"ANV-"+uuid.uuid4().hex[:8].upper()
    raise HTTPException(409,"No procurement slot is available in the next 30 days")

@app.post("/api/approve")
def approve(payload: ApproveIn, user=Depends(require_admin)):
    now=iso_now()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        a=con.execute("SELECT a.*,f.name,f.latitude,f.longitude,f.verified,f.priority_score,f.location AS farmer_location FROM applications a JOIN farmers f ON f.farmer_id=a.farmer_id WHERE a.application_id=?",(payload.application_id,)).fetchone()
        if not a: raise HTTPException(404,"Application not found")
        if not a["assigned_admin_id"]:
            assign_admin_for_application(con, payload.application_id)
            a=con.execute("SELECT a.*,f.name,f.latitude,f.longitude,f.verified,f.priority_score,f.location AS farmer_location FROM applications a JOIN farmers f ON f.farmer_id=a.farmer_id WHERE a.application_id=?",(payload.application_id,)).fetchone()
        if not a["assigned_admin_id"]:
            raise HTTPException(409,"No eligible non-local admin is available for this application")
        if user.get("admin_id") != a["assigned_admin_id"]:
            assigned=con.execute("SELECT admin_id,name,jurisdiction FROM admins WHERE admin_id=?",(a["assigned_admin_id"],)).fetchone()
            label=f"{assigned['name']} ({assigned['admin_id']})" if assigned else a["assigned_admin_id"]
            raise HTTPException(403,f"This application is assigned to {label}, not to the logged-in admin")
        if a["status"]!="Submitted": raise HTTPException(409,f"Application is already {a['status']}")
        r=con.execute("SELECT * FROM requirements WHERE id=?",(a["requirement_id"],)).fetchone()
        if not r or not r["active"]: raise HTTPException(409,"This application belongs to an old requirement")
        allocated=float(con.execute("SELECT COALESCE(SUM(allocation),0) FROM applications WHERE requirement_id=? AND status IN ('Approved','Scheduled','Completed')",(r["id"],)).fetchone()[0])
        remaining=max(0,float(r["required_quantity"])-allocated); capacity=max(0,float(r["centre_capacity"])-allocated)
        qty=float(payload.quantity) if payload.quantity is not None else min(float(a["quantity"]),remaining,capacity)
        if qty<=0 or qty>float(a["quantity"]) or qty>remaining or qty>capacity: raise HTTPException(400,"Approved quantity exceeds available requirement or capacity")
        date_display,slot_display,token=assign_schedule(con,payload.application_id,r["centre"],r["slot_capacity"])
        cur=con.execute("UPDATE applications SET status='Scheduled',allocation=?,centre=?,schedule_date=?,schedule_slot=?,token=?,reviewed_by_admin_id=?,reviewed_at=?,updated_at=? WHERE application_id=? AND status='Submitted'",
                    (qty,r["centre"],date_display,slot_display,token,user["admin_id"],now,now,payload.application_id))
        if cur.rowcount<1: raise HTTPException(409,"Application changed while approving; refresh and retry")
        con.execute("UPDATE admin_assignment_history SET decision='Approved',decision_at=? WHERE application_id=? AND admin_id=? AND decision IS NULL",
                    (now,payload.application_id,user["admin_id"]))
        offer=con.execute("SELECT quantity FROM crop_offers WHERE farmer_id=? AND lower(crop)=lower(?)",(a["farmer_id"],a["crop"])).fetchone()
        if offer:
            left=float(offer["quantity"])-qty
            if left>0: con.execute("UPDATE crop_offers SET quantity=?,updated_at=? WHERE farmer_id=? AND lower(crop)=lower(?)",(left,now,a["farmer_id"],a["crop"]))
            else: con.execute("DELETE FROM crop_offers WHERE farmer_id=? AND lower(crop)=lower(?)",(a["farmer_id"],a["crop"]))
        con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",(a["farmer_id"],"Approved & scheduled",f"{qty:g} qtl approved. {r['centre']} · {date_display} · {slot_display} · Token {token}.",now))
    return snapshot("FMR-1042", "admin")

@app.post("/api/schedule")
def schedule(payload: ScheduleIn, user=Depends(require_admin)):
    now=iso_now()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        a=con.execute("SELECT * FROM applications WHERE application_id=?",(payload.application_id,)).fetchone()
        if not a: raise HTTPException(404,"Application not found")
        if a["status"]=="Scheduled": return snapshot("FMR-1042", "admin")
        if a["assigned_admin_id"] and user.get("admin_id") != a["assigned_admin_id"]:
            raise HTTPException(403,"Only the assigned non-local admin can schedule this application")
        if a["status"]!="Approved": raise HTTPException(409,"Application must be approved before scheduling")
        r=con.execute("SELECT * FROM requirements WHERE id=?",(a["requirement_id"],)).fetchone()
        date_display,slot_display,token=assign_schedule(con,payload.application_id,r["centre"],r["slot_capacity"])
        con.execute("UPDATE applications SET status='Scheduled',schedule_date=?,schedule_slot=?,token=?,updated_at=? WHERE application_id=?",(date_display,slot_display,token,now,payload.application_id))
    return snapshot("FMR-1042", "admin")

@app.post("/api/complete")
def complete(payload: CompleteIn, user=Depends(require_admin)):
    now=iso_now()
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        a=con.execute("SELECT a.*,f.latitude,f.longitude,f.verified,f.priority_score,f.location FROM applications a JOIN farmers f ON f.farmer_id=a.farmer_id WHERE a.application_id=?",(payload.application_id,)).fetchone()
        if not a: raise HTTPException(404,"Application not found")
        if a["status"]!="Scheduled": raise HTTPException(409,"Only scheduled applications can be completed")
        if payload.actual_quantity>float(a["allocation"]): raise HTTPException(400,"Actual quantity cannot exceed allocation")
        r=con.execute("SELECT * FROM requirements WHERE id=?",(a["requirement_id"],)).fetchone()
        loc=calculate_location_score(a["latitude"],a["longitude"],r["latitude"],r["longitude"],a["location"],r["centre"])
        qfit=min(float(a["quantity"]),float(max(r["required_quantity"],1)))/float(max(r["required_quantity"],1))
        outcome=min(1.0,payload.actual_quantity/max(float(a["allocation"]),1e-9))
        con.execute("INSERT INTO model_history(application_id,crop_match,quantity_fit,location_match,verified,priority_score,outcome,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (a["application_id"],1.0,qfit,loc,float(a["verified"]),float(a["priority_score"]),outcome,now))
        con.execute("UPDATE applications SET status='Completed',updated_at=? WHERE application_id=?",(now,payload.application_id))
        con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",(a["farmer_id"],"Procurement completed",f"{payload.actual_quantity:g} qtl received at {r['centre']}.",now))
    return snapshot("FMR-1042", "admin")


@app.post("/api/chat")
def chat(payload: ChatIn, user=Depends(auth)):
    """Small rule-based assistant using only data the logged-in user is authorized to see."""
    msg=payload.message.strip().lower()
    snap=snapshot(user["farmer_id"] or "FMR-1042", user["role"])
    req=snap["requirement"]
    apps=snap["applications"]
    if user["role"]=="farmer":
        apps=[a for a in apps if a.get("farmer_id")==user["farmer_id"]]
        farmer=snap["farmers"][0] if snap["farmers"] else {}
        latest=apps[0] if apps else None
        if any(k in msg for k in ("hello","hi","hey","namaste")):
            reply="Hello! I can help with your crop application, eligibility, status, schedule, and procurement details."
        elif any(k in msg for k in ("status","application","approved","scheduled","completed")):
            if latest:
                reply=f"Your latest application {latest.get('application_id','')} is {latest.get('status','Submitted')}. "
                if latest.get("schedule_date"):
                    reply+=f"Scheduled for {latest['schedule_date']} at {latest.get('schedule_slot','')}. "
                if latest.get("token"): reply+=f"Your token is {latest['token']}."
            else:
                reply="You do not have an application yet. Use “Submit Crop” to register your crop availability."
        elif any(k in msg for k in ("eligible","eligibility","qualify","qualified")):
            crop=(latest or {}).get("crop") or farmer.get("crop") or "your crop"
            verified=bool(farmer.get("verified"))
            match=str(crop).strip().lower()==str(req.get("crop","")).strip().lower()
            qty=float((latest or {}).get("quantity") or farmer.get("qty") or 0)
            reply=f"Eligibility currently depends on a verified farmer account, a crop matching the active requirement ({req.get('crop')}), and positive available quantity. "
            reply+=f"Your account is {'verified' if verified else 'not yet verified'}, and your crop is {'a match' if match else 'not a match'}."
            if qty<=0: reply+=" Please register a positive crop quantity."
        elif any(k in msg for k in ("schedule","slot","token","when","date")):
            if latest and latest.get("schedule_date"):
                reply=f"Your procurement slot is {latest['schedule_date']} at {latest.get('schedule_slot','')}, {latest.get('centre','')}. Token: {latest.get('token','—')}."
            else:
                reply="A procurement slot is assigned automatically when your application is approved."
        elif any(k in msg for k in ("requirement","demand","needed","remaining","crop")):
            reply=f"The active requirement is {req.get('crop')} for {req.get('needed',0)} qtl. Remaining requirement: {req.get('remaining',0)} qtl at {req.get('centre')}."
        else:
            reply="I can answer: “What is my status?”, “Am I eligible?”, “When is my schedule?”, or “What is the current requirement?”"
    else:
        if any(k in msg for k in ("hello","hi","hey","namaste")):
            reply="Hello! I can summarize the active requirement, matching workflow, and application status."
        elif any(k in msg for k in ("requirement","demand","remaining","crop")):
            reply=f"Active requirement: {req.get('crop')} — {req.get('needed',0)} qtl needed, {req.get('remaining',0)} qtl remaining. Centre: {req.get('centre')}."
        elif any(k in msg for k in ("how","eligible","eligibility","matching","ml","score")):
            reply="Matching considers crop match, quantity fit, location match, verification, and priority. Verified farmers with the required crop and positive quantity are eligible for ML ranking."
        elif any(k in msg for k in ("pending","submitted","scheduled","completed","applications")):
            counts={}
            for a in apps: counts[a.get("status","Unknown")]=counts.get(a.get("status","Unknown"),0)+1
            summary=", ".join(f"{k}: {v}" for k,v in counts.items()) or "no applications"
            reply=f"Application summary: {summary}."
        else:
            reply="I can answer: “What is the current requirement?”, “How does ML matching work?”, or “Show application status summary.”"
    return {"reply":reply}

@app.post("/api/farmers/{farmer_id}/verify")
def verify_farmer(farmer_id: str, user=Depends(require_admin)):
    with db() as con:
        row=con.execute("SELECT farmer_id FROM farmers WHERE farmer_id=?",(farmer_id,)).fetchone()
        if not row: raise HTTPException(404,"Farmer not found")
        con.execute("UPDATE farmers SET verified=1 WHERE farmer_id=?",(farmer_id,))
        con.execute("INSERT INTO notifications(farmer_id,title,message,created_at) VALUES(?,?,?,?)",(farmer_id,"Verification completed","Your farmer account has been verified for procurement matching.",iso_now()))
    return snapshot("FMR-1042", "admin")

@app.post("/api/reset")
def reset(user=Depends(require_admin)):
    if DB_PATH.exists(): DB_PATH.unlink()
    init_db(); return snapshot("FMR-1042", "admin")

@app.get("/")
def home(): return FileResponse(ROOT/'index.html',headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/app.js")
def app_js():
    # index.html loads this as an external <script src="app.js">. It used to be
    # inlined directly into index.html because no route served this file, which
    # left this standalone copy stale and unused. Serving it here is the fix.
    return FileResponse(ROOT/'app.js', media_type="application/javascript",
                         headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8000,reload=False)
