# Pedidos MVP (WhatsApp/Instagram -> Link para site de pedidos)

Este é um MVP para receber pedidos via um **link único** que você envia automaticamente no WhatsApp/Instagram.
Frontend simples (HTML) + Backend FastAPI com SQLite.

## 1) Como rodar no Windows (local)

1. Instale Python 3.11+.
2. Abra o **PowerShell** e execute:

```powershell
cd pedidos-mvp
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.app:app --reload --port 8000
```

3. Abra `frontend\index.html` no navegador (duplo clique).

- A página usa `http://127.0.0.1:8000` como API.
- Os produtos iniciais são semeados automaticamente no primeiro run.

## 2) Teste rápido

- Clique em **Adicionar** para incluir itens no carrinho.
- Preencha **nome, telefone e endereço**.
- Clique **Finalizar pedido**.
- Você verá a confirmação com **ID do pedido** e **total**.

## 3) Pagamento (opcional)

- Para ativar Stripe, defina `STRIPE_ENABLED = True` em `backend/app.py` e configure a variável de ambiente `STRIPE_SECRET`.
- Em produção, ajuste `CHECKOUT_SUCCESS_URL` e `CHECKOUT_CANCEL_URL`.

## 4) Próximos passos

- Hospedar o **backend** (Render, Railway, Fly.io).
- Hospedar o **frontend** (Vercel, Netlify) e apontar `API` no `index.html` para a URL do backend.
- Configurar WhatsApp Cloud API/Instagram para responder automaticamente com o link do site.
