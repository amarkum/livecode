"""Writes design.html (the design) and app.html (the running app) for the design-compare tests.

Both show one orders page: a header, a filters form and a grid of order cards, each with a status pill in
its own colours (Paid green, Due amber, Cancelled red, Sold blue). The app shows its own data: other
names, prices and avatars, one more order, and the statuses in another order. It also differs from the
design in a few real ways, which the tests expect to be found: the New order and Apply buttons are green,
the title is larger, the cards are square-cornered with less padding, the inputs are darker and squarer,
the Cancelled status is grey instead of red, and its Cancel and Save buttons are the other way round.

Run it again after changing it: python tests/fixtures/design_demo/make_pages.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

STATUS = {"Paid": ("#dcfce7", "#166534"), "Due": ("#fef3c7", "#92400e"), "Cancelled": ("#fee2e2", "#991b1b"), "Sold": ("#dbeafe", "#1e40af")}


def avatar(color):
    return ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='48' height='48'>"
            f"<circle cx='24' cy='24' r='22' fill='%23{color}'/><rect x='12' y='20' width='24' height='8' rx='2' fill='white'/></svg>")


def page(cards, *, button, radius, pad, input_border, input_radius, title, status=STATUS, actions=("Cancel", "Save")):
    items = "".join(
        f"""<article class="card"><img class="avatar" src="{avatar(c)}" width="48" height="48" alt="">
      <div class="body"><div class="name">{n}</div><div class="price">{p}</div></div><span class="pill {st.lower()}">{st}</span></article>"""
        for n, p, c, st in cards)
    buttons = "".join(f'<button type="button" class="{"secondary" if a == "Cancel" else "primary"}">{a}</button>' for a in actions)
    pills = "\n".join(f".pill.{k.lower()} {{ background: {bg}; color: {fg}; }}" for k, (bg, fg) in status.items())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Orders</title><style>
body {{ margin: 0; font-family: Arial, Helvetica, sans-serif; background: #f4f5f7; color: #1c1e21; }}
header {{ background: #1f2937; color: #fff; padding: 18px 32px; display: flex; align-items: center; justify-content: space-between; }}
h1 {{ margin: 0; font-size: {title}px; font-weight: 700; }}
button {{ background: {button}; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px; font-size: 14px; }}
#orders-mfe {{ padding: 24px 32px; }}
form.filters {{ display: flex; gap: 12px; align-items: center; background: #fff; padding: 16px; border-radius: 12px; margin-bottom: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
form.filters input {{ width: 260px; height: 38px; box-sizing: border-box; padding: 0 12px; font-size: 14px;
  border: 1px solid {input_border}; border-radius: {input_radius}px; background: #fff; }}
.grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; }}
.card {{ background: #fff; border-radius: {radius}px; padding: {pad}px; display: flex; gap: 16px; align-items: center;
  box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
.body {{ flex: 1; }} .name {{ font-size: 16px; font-weight: 600; }} .price {{ font-size: 14px; color: #6b7280; margin-top: 4px; }}
.pill {{ font-size: 12px; padding: 4px 10px; border-radius: 999px; }}
.actions {{ display: flex; gap: 12px; justify-content: flex-end; margin-top: 24px; }}
button.secondary {{ background: #e5e7eb; color: #111827; }}
{pills}
</style></head><body>
<header><h1>Orders</h1><button>New order</button></header>
<div id="orders-mfe">
  <form class="filters"><input class="search" placeholder="Search orders"><input class="date" placeholder="Any date"><button type="button">Apply</button></form>
  <div class="grid">{items}</div>
  <div class="actions">{buttons}</div>
</div>
</body></html>
"""


DESIGN = page([("Jane Cooper", "$1,240.00", "f59e0b", "Paid"), ("Wade Warren", "$820.50", "10b981", "Cancelled"),
               ("Esther Howard", "$3,020.00", "3b82f6", "Sold")],
              button="#2563eb", radius=12, pad=20, input_border="#d1d5db", input_radius=8, title=22)
APP = page([("Amar Kumar", "$99.00", "ef4444", "Sold"), ("Priya Sharma", "$15,000.75", "8b5cf6", "Paid"), ("Sam Lee", "$0.00", "14b8a6", "Cancelled"),
            ("Ava Chen", "$410.20", "f97316", "Due")],
           button="#16a34a", radius=4, pad=14, input_border="#6b7280", input_radius=2, title=28,
           status={**STATUS, "Cancelled": ("#f3f4f6", "#374151")}, actions=("Save", "Cancel"))

if __name__ == "__main__":
    for name, text in (("design.html", DESIGN), ("app.html", APP)):
        with open(os.path.join(HERE, name), "w") as handle:
            handle.write(text)
    print("wrote design.html and app.html")
