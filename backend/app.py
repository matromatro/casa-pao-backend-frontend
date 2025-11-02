from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from typing import List, Literal, Optional
import sqlite3, os, datetime, csv, io, json

# ===== Integração com Google Sheets =====
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
print("DEBUG SHEETS ID:", GOOGLE_SHEETS_ID)

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")


# =====================
# Configurações gerais
# =====================
STRIPE_ENABLED = False
STRIPE_SECRET = os.getenv("STRIPE_SECRET", "")
CHECKOUT_SUCCESS_URL = os.getenv("CHECKOUT_SUCCESS_URL", "https://example.com/sucesso")
CHECKOUT_CANCEL_URL = os.getenv("CHECKOUT_CANCEL_URL", "https://example.com/cancelado")

# Banco (use um Disk no Render e a env DB_PATH=/var/data/data.db para persistir)
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

app = FastAPI(title="Casa do pão francês — Pedidos API")

# CORS (inclui X-Admin-Token e OPTIONS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # opcional: restrinja ao seu domínio da Vercel
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "X-Admin-Token"],
    expose_headers=["*"],
)

# ===== Middleware para logar erros e retornar detalhe (diagnóstico) =====
@app.middleware("http")
async def catch_all_exceptions(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        import traceback, sys
        print("### SERVER ERROR ###", file=sys.stderr)
        traceback.print_exc()
        return JSONResponse({"error": "server", "detail": str(e)}, status_code=500)

# ===== OPTIONS catch-all p/ preflight CORS com header custom =====
@app.options("/{rest_of_path:path}")
def options_catch_all(rest_of_path: str = ""):
    return PlainTextResponse("", status_code=200)

# =====================
# Banco de dados (SQLite)
# =====================
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

# Tabelas base (mínimas); colunas novas serão adicionadas por migrações abaixo
cur.execute("""
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, name TEXT, price REAL
)""")
cur.execute("""
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_name TEXT, customer_phone TEXT, customer_address TEXT,
  total REAL
)""")
cur.execute("""
CREATE TABLE IF NOT EXISTS order_items(
  order_id INTEGER, product_id INTEGER, qty INTEGER, price REAL
)""")
conn.commit()

# ---- MIGRAÇÕES: adiciona colunas que podem faltar em bancos antigos ----
def _safe_add_column(table: str, col: str, coltype: str):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        conn.commit()
    except Exception:
        pass  # já existe ou não precisa

_safe_add_column("orders", "checkout_url", "TEXT")
_safe_add_column("orders", "mode", "TEXT")
_safe_add_column("orders", "delivery_date", "TEXT")
_safe_add_column("orders", "status", "TEXT")

# Re-seed dos produtos oficiais (somente os dois corretos)
cur.execute("DELETE FROM products")
cur.executemany("INSERT INTO products(id,name,price) VALUES(?,?,?)", [
    (1, "Pacote (10 pães) — retirada na loja — saco a vácuo", 5.00),
    (2, "Entrega — 20 pães (2×10) — saco a vácuo (sexta-feira)", 14.00),
])
conn.commit()

# =====================
# Modelos
# =====================
class ItemIn(BaseModel):
    id: int
    qty: int = Field(ge=1)

class Customer(BaseModel):
    nome: str
    telefone: str
    endereco: Optional[str] = ""

class OrderIn(BaseModel):
    customer: Customer
    items: List[ItemIn]
    mode: Literal["pickup", "delivery"]

# =====================
# Utilidades
# =====================
def next_friday(today: Optional[datetime.date] = None) -> datetime.date:
    if today is None:
        today = datetime.date.today()
    # Monday=0 ... Sunday=6, Friday=4
    days_ahead = (4 - today.weekday()) % 7
    return today if days_ahead == 0 else today + datetime.timedelta(days=days_ahead)

# =====================
# Rotas públicas
# =====================
@app.get("/")
def root():
    return {"ok": True, "service": "Casa do pão francês — Pedidos API"}

@app.get("/products")
def get_products():
    rows = cur.execute("SELECT id,name,price FROM products").fetchall()
    return [{"id": r[0], "name": r[1], "price": r[2]} for r in rows]

@app.post("/orders")
def create_order(payload: OrderIn):
    if not payload.items:
        return {"error": "Carrinho vazio"}

    # Regras:
    # - delivery: somente produto ID 2 (20 pães), endereço obrigatório, entrega na próxima sexta
    # - pickup:   somente produto ID 1 (pacote de 10), sem endereço obrigatório
    allowed_id = 2 if payload.mode == "delivery" else 1
    invalid = [it.id for it in payload.items if it.id != allowed_id]
    if invalid:
        return {"error": "Produtos incompatíveis com o modo selecionado."}

    if payload.mode == "delivery":
        if not payload.customer.endereco or not payload.customer.endereco.strip():
            return {"error": "Endereço é obrigatório para entrega."}
        entrega = next_friday().isoformat()
    else:
        entrega = None

    # Preços oficiais do DB
    ids = tuple({i.id for i in payload.items})
    qmarks = ",".join(["?"] * len(ids))
    db_products = {r[0]: (r[1], r[2]) for r in cur.execute(
        f"SELECT id,name,price FROM products WHERE id IN ({qmarks})", ids
    ).fetchall()}

    total = 0.0
    for it in payload.items:
        if it.id not in db_products:
            return {"error": "Produto inválido"}
        total += db_products[it.id][1] * it.qty

    checkout_url = None  # Stripe desligado no MVP

    # Grava pedido
    cur.execute("""
        INSERT INTO orders(customer_name,customer_phone,customer_address,total,checkout_url,mode,delivery_date,status)
        VALUES(?,?,?,?,?,?,?,?)
    """, (payload.customer.nome, payload.customer.telefone, payload.customer.endereco or "",
          total, checkout_url, payload.mode, entrega, "pending"))
    order_id = cur.lastrowid

    for it in payload.items:
        _, price = db_products[it.id]
        cur.execute("INSERT INTO order_items(order_id,product_id,qty,price) VALUES(?,?,?,?)",
                    (order_id, it.id, it.qty, price))
    conn.commit()
    # Envia automaticamente para Google Sheets (se configurado)
    _append_to_gsheet_safe(order_id, payload, db_products, total, entrega)


    return {"order_id": order_id, "total": total, "checkout_url": checkout_url,
            "mode": payload.mode, "delivery_date": entrega}

# =====================
# Admin / Exportação / Status
# =====================

def _append_to_gsheet(row):
    """Adiciona uma linha na planilha Google Sheets"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    try:
        from google.oauth2.service_account import Credentials
        import gspread
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            info, scopes=["https://docs.google.com/spreadsheets/d/e/2PACX-1vROixlMMZdI-t4Bc-4yBC5fZVggwFz_zk7ERpjO0bSiSz3tE5JQxZM83LcELU0IfdoNo2nIS3UlWU3z/pubhtml"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEETS_ID)
        ws = sh.sheet1
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print("GSHEETS ERROR:", e)

def _append_to_gsheet_safe(order_id: int, payload, db_products: dict, total: float, entrega: str | None):
    """Prepara e envia o pedido para o Google Sheets"""
    try:
        items_join = "; ".join([f"{db_products[it.id][0]} x{it.qty}" for it in payload.items])
        _append_to_gsheet([
            order_id,
            payload.customer.nome,
            payload.customer.telefone,
            payload.customer.endereco or "",
            total,
            payload.mode,
            entrega or "",
            "pending",
            items_join
        ])
    except Exception as e:
        print("GSHEETS APPEND ERROR:", e)




ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

def require_admin(x_admin_token: Optional[str] = Header(None)) -> bool:
    return bool(ADMIN_TOKEN) and (x_admin_token == ADMIN_TOKEN)

# Versão segura de /orders (sem GROUP_CONCAT)
@app.get("/orders")
def list_orders(x_admin_token: Optional[str] = Header(None), limit: int = 200):
    if not require_admin(x_admin_token):
        return {"error": "unauthorized"}
    try:
        rows = cur.execute(
            "SELECT id, customer_name, customer_phone, customer_address, total, mode, "
            "COALESCE(delivery_date,''), COALESCE(status,'pending') "
            "FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

        orders = []
        for r in rows:
            oid, name, phone, addr, total, mode, delivery_date, status = r
            items_rows = cur.execute(
                "SELECT p.name, i.qty FROM order_items i "
                "JOIN products p ON p.id = i.product_id WHERE i.order_id = ?",
                (oid,)
            ).fetchall()
            items = "; ".join([f"{n} x{q}" for n, q in items_rows]) if items_rows else ""
            orders.append({
                "id": oid,
                "customer_name": name,
                "customer_phone": phone,
                "customer_address": addr,
                "total": total,
                "mode": mode,
                "delivery_date": delivery_date,
                "status": status or "pending",
                "items": items,
            })
        return orders
    except Exception as e:
        return {"error": "server", "detail": str(e)}

@app.get("/orders.csv")
def export_orders_csv(x_admin_token: Optional[str] = Header(None), limit: int = 1000):
    if not require_admin(x_admin_token):
        return {"error": "unauthorized"}
    try:
        rows = cur.execute(
            "SELECT id, customer_name, customer_phone, customer_address, total, mode, "
            "COALESCE(delivery_date,''), COALESCE(status,'pending') "
            "FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id","customer_name","customer_phone","customer_address",
                         "total","mode","delivery_date","status","items"])
        for r in rows:
            oid, name, phone, addr, total, mode, delivery_date, status = r
            items_rows = cur.execute(
                "SELECT p.name, i.qty FROM order_items i "
                "JOIN products p ON p.id = i.product_id WHERE i.order_id = ?",
                (oid,)
            ).fetchall()
            items = "; ".join([f"{n} x{q}" for n, q in items_rows]) if items_rows else ""
            writer.writerow([oid, name, phone, addr, total, mode, delivery_date, status or "pending", items])
        return output.getvalue()
    except Exception as e:
        return {"error": "server", "detail": str(e)}

class StatusIn(BaseModel):
    status: str  # 'done' ou 'pending'

@app.post("/orders/{order_id}/status")
def update_status(order_id: int, payload: StatusIn, x_admin_token: Optional[str] = Header(None)):
    if not require_admin(x_admin_token):
        return {"error": "unauthorized"}
    if payload.status not in ("done", "pending"):
        return {"error": "invalid status"}
    cur.execute("UPDATE orders SET status=? WHERE id=?", (payload.status, order_id))
    conn.commit()
    return {"ok": True, "id": order_id, "status": payload.status}
