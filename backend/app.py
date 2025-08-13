from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Literal, Optional
import sqlite3, os, datetime

# ---------------- Config ----------------
STRIPE_ENABLED = False  # mude para True quando configurar Stripe
STRIPE_SECRET = os.getenv("STRIPE_SECRET", "")
CHECKOUT_SUCCESS_URL = "http://127.0.0.1:8000/sucesso"  # ajuste no deploy
CHECKOUT_CANCEL_URL = "http://127.0.0.1:8000/cancelado"

# Caminho para o DB no mesmo diretório do app.py
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

app = FastAPI(title="Pedidos API — Casa do pão francês")

# Libera CORS para testes locais (ajuste no deploy)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DB mínimo ----------------
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

# Re-seed produtos (limpa e cria os dois produtos certos)
cur.execute("DELETE FROM products")
cur.executemany("INSERT INTO products(id,name,price) VALUES(?,?,?)", [
    (1, "Pacote (10 pães) — retirada na loja — saco a vácuo", 5.00),
    (2, "Entrega — 20 pães (2×10) — saco a vácuo (sexta-feira)", 14.00),
])
conn.commit()

# ---------------- Modelos ----------------
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

def next_friday(today: datetime.date | None = None) -> datetime.date:
    if today is None:
        today = datetime.date.today()
    days_ahead = (4 - today.weekday()) % 7  # 4 = sexta-feira
    if days_ahead == 0:
        return today
    return today + datetime.timedelta(days=days_ahead)

# ---------------- Rotas ----------------
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

    checkout_url = None
    if STRIPE_ENABLED and STRIPE_SECRET:
        try:
            import stripe
            stripe.api_key = STRIPE_SECRET
            line_items = []
            for it in payload.items:
                name, price = db_products[it.id]
                line_items.append({
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": name},
                        "unit_amount": int(round(price*100))
                    },
                    "quantity": it.qty
                })
            sess = stripe.checkout.Session.create(
                mode="payment",
                line_items=line_items,
                success_url=CHECKOUT_SUCCESS_URL,
                cancel_url=CHECKOUT_CANCEL_URL
            )
            checkout_url = sess.url
        except Exception as e:
            return {"error": f"Falha ao criar sessão de pagamento: {e}"}

    cur.execute("""INSERT INTO orders(customer_name,customer_phone,customer_address,total,checkout_url,mode,delivery_date)
                   VALUES(?,?,?,?,?,?,?)""",
                (payload.customer.nome, payload.customer.telefone, payload.customer.endereco or "", total, checkout_url, payload.mode, entrega))
    order_id = cur.lastrowid
    for it in payload.items:
        _, price = db_products[it.id]
        cur.execute("INSERT INTO order_items(order_id,product_id,qty,price) VALUES(?,?,?,?)",
                    (order_id, it.id, it.qty, price))
    conn.commit()

    return {"order_id": order_id, "total": total, "checkout_url": checkout_url, "mode": payload.mode, "delivery_date": entrega}
