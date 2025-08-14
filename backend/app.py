from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Literal, Optional
import sqlite3, os, datetime, csv, io, json

# =====================
# Configurações gerais
# =====================
STRIPE_ENABLED = False
STRIPE_SECRET = os.getenv("STRIPE_SECRET", "")
CHECKOUT_SUCCESS_URL = os.getenv("CHECKOUT_SUCCESS_URL", "https://example.com/sucesso")
CHECKOUT_CANCEL_URL = os.getenv("CHECKOUT_CANCEL_URL", "https://example.com/cancelado")

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

app = FastAPI(title="Casa do pão francês — Pedidos API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*", "X-Admin-Token"],
    expose_headers=["*"],
)

# ===== Middleware para logar erros e retornar detalhe (diagnóstico) =====
from fastapi.responses import JSONResponse, PlainTextResponse

@app.middleware("http")
async def catch_all_exceptions(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        # log no console do Render
        import traceback, sys
        print("### SERVER ERROR ###", file=sys.stderr)
        traceback.print_exc()
        # resposta amigável p/ debug no front
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

cur.execute("""CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, name TEXT, price REAL
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_name TEXT, customer_phone TEXT, customer_address TEXT,
  total REAL, checkout_url TEXT,
  mode TEXT,
  delivery_date TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS order_items(
  order_id INTEGER, product_id INTEGER, qty INTEGER, price REAL
)""")
conn.commit()

# ---- MIGRAÇÃO: garante coluna 'status' em orders ----
try:
    cur.execute("ALTER TABLE orders ADD COLUMN status TEXT")
    conn.commit()
except Exception:
    pass  # já existe

# Re-seed produtos corretos
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
    days_ahead = (4 - today.weekday()) % 7  # sexta=4
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
    return [{"id":r[0], "name":r[1], "price":r[2]} for r in rows]

@app.post("/orders")
def create_order(payload: OrderIn):
    if not payload.items:
        return {"error": "Carrinho vazio"}

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

    ids = tuple({i.id for i in payload.items})
    qmarks = ",".join(["?"]*len(ids))
    db_products = {r[0]: (r[1], r[2]) for r in cur.execute(
        f"SELECT id,name,price FROM products WHERE id IN ({qmarks})", ids
    ).fetchall()}

    total = 0.0
    for it in payload.items:
        if it.id not in db_products:
            return {"error":"Produto inválido"}
        total += db_products[it.id][1] * it.qty

    checkout_url = None  # Stripe desligado no MVP

    cur.execute("""INSERT INTO orders(customer_name,customer_phone,customer_address,total,checkout_url,mode,delivery_date,status)
                   VALUES(?,?,?,?,?,?,?,?)""",                (payload.customer.nome, payload.customer.telefone, payload.customer.endereco or "", total, checkout_url, payload.mode, entrega, "pending"))
    order_id = cur.lastrowid
    for it in payload.items:
        _, price = db_products[it.id]
        cur.execute("INSERT INTO order_items(order_id,product_id,qty,price) VALUES(?,?,?,?)",                    (order_id, it.id, it.qty, price))
    conn.commit()

    return {"order_id": order_id, "total": total, "checkout_url": checkout_url, "mode": payload.mode, "delivery_date": entrega}

# =====================
# Admin / Exportação / Status
# =====================
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

def require_admin(x_admin_token: Optional[str] = Header(None)) -> bool:
    return bool(ADMIN_TOKEN) and (x_admin_token == ADMIN_TOKEN)

# Versão SEGURA de /orders (sem GROUP_CONCAT)
@app.get("/orders")
def list_orders(x_admin_token: Optional[str] = Header(None), limit: int = 200):
    if not require_admin(x_admin_token):
        return {"error": "unauthorized"}
    try:
        rows = cur.execute("""            SELECT id, customer_name, customer_phone, customer_address,
                   total, mode, COALESCE(delivery_date,''), COALESCE(status,'pending')
            FROM orders
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()

        orders = []
        for r in rows:
            oid, name, phone, addr, total, mode, delivery_date, status = r
            items_rows = cur.execute("""                SELECT p.name, i.qty
                FROM order_items i
                JOIN products p ON p.id = i.product_id
                WHERE i.order_id = ?
            """, (oid,)).fetchall()
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

# CSV usando a mesma lógica segura (sem GROUP_CONCAT)
@app.get("/orders.csv")
def export_orders_csv(x_admin_token: Optional[str] = Header(None), limit: int = 1000):
    if not require_admin(x_admin_token):
        return {"error": "unauthorized"}
    try:
        rows = cur.execute(            "SELECT id, customer_name, customer_phone, customer_address, total, mode, COALESCE(delivery_date,''), COALESCE(status,'pending') FROM orders ORDER BY id DESC LIMIT ?",            (limit,)        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id","customer_name","customer_phone","customer_address","total","mode","delivery_date","status","items"])
        for r in rows:
            oid, name, phone, addr, total, mode, delivery_date, status = r
            items_rows = cur.execute("""                SELECT p.name, i.qty
                FROM order_items i
                JOIN products p ON p.id = i.product_id
                WHERE i.order_id = ?
            """, (oid,)).fetchall()
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
