# UX-017 — Token e scale: il pannello con un solo sistema visivo

Stato: contratto di design, prima della build. Origine: l'audit «OmniaCLI contro il DX Playbook di Toyota Europe» del 23/11/2026 (`scratchpad/dx-audit/REPORT.md`, misure in `measure.json` e `dom-metrics.json`, schermate in `shots/`), misurato su `carlo_dev_2026` a `4543ded`. **Decisione di Carlo, 23/11/2026**: si applicano tutti e dieci i cambi; i link di riga e i nomi delle istanze nel colore del testo (sottolineati al passaggio), il rosso solo per il primario e il pericolo.

Lo implementa chi tocca `omniacli/templates/base.html` (token e componenti), un partial nuovo `_tokens.html` (incluso da `base.html`, `login.html`, `invite.html`), `_icons.html` (macro icona e sprite), `omniacli/brand.py` (`css()`), i 77 template per le sostituzioni meccaniche, `omniacli/i18n/it/*.json` (le stringhe che perdono il glifo) e i test `tests/test_whitelabel.py`, `tests/test_addresses.py`. Valgono le regole di UX-001…016 (un solo primario per vista, aiuto al passaggio su ogni pulsante, `test_the_page_explains_itself`); questo contratto non le ripete.

Il canone è quello di `~/.claude/rules/odoo-ux.md`, sezione «Dove i pixel sono nostri»: qui i pixel sono tutti nostri, quindi le regole visive valgono per intero e si applicano **con i token, mai con valori fissi**.

---

## 0. Gate

UI utente non banale: **sì**. Cambia l'aspetto di ogni schermo del pannello, del login e dell'invito, per tutte le persone. Non cambia la IA di nessuno schermo: niente schede, campi o sezioni aggiunti o tolti (quelli restano dei contratti UX-001…016). Il rischio di questo lavoro non è l'accrescimento, è la **deriva**: 18 corpi, 190 spaziature, 9 varianti di pulsante sono nati un template alla volta. Il contratto chiude le scale, così il template successivo non può riaprirle.

## 1. Persone e device

| Persona | Chi è nel codice | Device | Cosa conta qui |
|---|---|---|---|
| P1 Integratore (Carlo) sul centrale | `is_admin`, non `scoped` | desktop 1440×900, chiaro e scuro | leggere stati e nomi in fretta su liste dense; il rosso che vuol dire «attenzione» e nient'altro |
| P1b Admin di un server | `is_admin`, non `scoped` | desktop; telefono per «è andata stanotte?» | come P1 |
| P2 Admin del workspace (il cliente) | `scoped` | desktop e telefono 375-400 | il pannello col **suo** marchio (whitelabel); fuoco e campi leggibili |
| P3 Sviluppatore | ruolo `developer` | desktop | log e console leggibili |
| Chi usa solo la tastiera | tutte | desktop | fuoco sempre visibile, schede con le frecce, X nei dialoghi |

**Lavoro primario** di questo contratto: in ogni schermo, al primo sguardo, si distinguono **l'azione primaria**, **ciò che è pericoloso** e **lo stato**; tutto il resto è neutro.

## 2. Il set di token (in `_tokens.html`, scritto una volta)

### 2.1 Dove stanno

Un partial `omniacli/templates/_tokens.html` contiene l'unico `<style>` dei token. Lo includono `base.html` (al posto delle righe 51-103), `login.html` e `invite.html` (al posto delle loro copie, righe 14-40 circa di ciascuno). Dopo i token, `brand.css` (whitelabel) resta l'ultimo `<style>` dell'`<head>`, come oggi.

**Lo scuro si scrive una volta** (D1, decisa qui): nel partial i valori scuri stanno in un solo `{% set dark %}…{% endset %}` Jinja, emesso due volte, in `:root[data-theme="dark"]` e in `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`. Una sorgente, nessuna dipendenza da browser nuovi, i test di whitelabel che cercano `--accent: #DC2626` / `--accent: #F87171` restano veri. Alternativa scartata: `light-dark()` (Chrome 123, Safari 17.5): più elegante, ma cambia le stringhe che `brand.py` e i test emettono e non dà niente in più alla persona.

### 2.2 Primitivi (valori fissi; un componente non li usa mai direttamente; un whitelabel non li sovrascrive)

```css
/* neutri chiari */      --gray-0:#FFFFFF; --gray-25:#F9FAFB; --gray-50:#F5F6F8; --gray-100:#E4E7EC; --gray-200:#D0D5DD;
                         --gray-400:#868E9C; --gray-500:#5F6673; --gray-900:#111827;
/* neutri scuri */       --ink-950:#0A0D12; --ink-900:#0E1117; --ink-850:#161B22; --ink-800:#1C222B; --ink-700:#2A313C;
                         --ink-650:#3A4350; --ink-500:#667085; --ink-300:#98A2B3; --ink-150:#D6DBE4; --ink-100:#E6EAF0;
/* barra in alto */      --slate-900:#0F172A; --slate-800:#1E293B; --slate-300:#CBD5E1; --slate-250:#C3CAD5; --slate-400:#8794A6;
/* rosso */              --red-50:#FEF2F2; --red-75:#FEF3F2; --red-400:#F87171; --red-600:#DC2626; --red-700:#B91C1C;
                         --red-750:#B42318; --red-800:#912018; --red-950:#2A1517;
/* verde, ambra, blu */  --green-50:#ECFDF3; --green-700:#15803D; --green-400:#4ADE80; --green-950:#0F2A1A;
                         --amber-50:#FFFAEB; --amber-700:#B54708; --amber-400:#FBBF24; --amber-950:#2A2110;
                         --blue-50:#EFF6FF; --blue-700:#1D4ED8; --blue-400:#7AA2FF; --blue-950:#141E33;
/* console */            --console-err:#FF7B7B; --console-warn:#F0C060;
```
I `--cat-2…8` e `--series-1/2` dei grafici restano come sono (validati sul validatore dataviz): sono token di dati, non entrano in questa revisione.

### 2.3 Semantici: i nomi di oggi restano

I 30 nomi semantici di oggi (`--bg`, `--panel`, `--text`, `--muted`, `--border`…) sono usati ~700 volte in 77 template e da `brand.py`. **Non si rinominano** (少: rinominare è diff senza valore per la persona). Cambiano i valori dove l'audit lo chiede, se ne aggiungono pochi dove mancava il ruolo, se ne tolgono cinque.

| Token | Ruolo | Chiaro | Scuro | Note |
|---|---|---|---|---|
| `--bg` | sfondo pagina (60%) | gray-50 | ink-900 | |
| `--panel` | card, dialoghi, campi | gray-0 | ink-850 | |
| `--panel-2` | intestazioni tabella, tile, hover di riga | gray-25 | ink-800 | |
| `--text` | testo, link, nomi | gray-900 | ink-100 | |
| `--muted` | testo secondario | **gray-500 #5F6673** (era #6B7280) | ink-300 | cambio 9 |
| `--fg-disabled` **nuovo** | solo per il disabilitato | ink-300 | ink-500 | esente dal contrasto (WCAG), mai per testo attivo |
| `--border` | divisori decorativi | gray-100 | ink-700 | |
| `--border-strong` | bordo dei pulsanti secondari, hover delle card | gray-200 | ink-650 | decorativo |
| `--border-input` **nuovo** | bordo di campi, select, checkbox | **gray-400 #868E9C** | **ink-500 #667085** | cambio 1 (≥ 3:1) |
| `--accent` | **solo** il primario | red-600 | red-400 | whitelabel |
| `--accent-hover` **nuovo** | hover del primario | `color-mix(in srgb, var(--accent) 88%, #000)` | stesso | segue il whitelabel da solo |
| `--accent-soft` | sfondo tenue del primario (raro) | red-50 | red-950 | whitelabel |
| `--on-accent` | testo sul primario | #fff | gray-900 | whitelabel |
| `--accent-strong` **nuovo** | anello di fuoco | red-700 #B91C1C | red-400 | whitelabel (sostituisce `--link`) |
| `--focus` **nuovo** | colore del fuoco | `var(--accent-strong)` | stesso | nella `.topbar`: `var(--brand-top)` |
| `--link` | link | **`var(--text)`** (era #B91C1C) | `var(--text)` | decisione di Carlo; il link si distingue per la sottolineatura |
| `--danger` **nuovo** | fondo del pulsante che distrugge | red-750 | red-750 | **fisso**, mai dal whitelabel |
| `--danger-hover` **nuovo** | | red-800 | red-800 | |
| `--on-danger` **nuovo** | | #fff | #fff | |
| `--err` / `--err-soft` | testo e fondo d'errore, contorno del secondario pericoloso | red-750 / red-75 | red-400 / red-950 | |
| `--ok` / `--ok-soft` | | green-700 / green-50 | green-400 / green-950 | |
| `--warn` / `--warn-soft` | | amber-700 / amber-50 | amber-400 / amber-950 | |
| `--info` / `--info-soft` | | blue-700 / blue-50 | blue-400 / blue-950 | |
| `--test` | marca test/staging | `var(--info)` | `var(--info)` | alias |
| `--selected` **nuovo** | stato scelto: casella spuntata, segmento premuto, card scelta, scheda attiva | `var(--text)` | `var(--text)` | neutro: niente rosso per «scelto» |
| `--on-selected` **nuovo** | testo su `--selected` | `var(--panel)` | `var(--panel)` | |
| `--side-bg` / `--side-text` / `--side-hover` / `--side-current` | barra in alto | slate-900 / slate-300 / slate-800 / #fff | ink-950 / slate-250 / ink-850 / #fff | |
| `--side-muted` | lingua non scelta | **slate-400 #8794A6** (era #64748B) | **slate-400** (era #6B7685) | cambio 9 |
| `--brand-top` **ora in :root** | marchi sulla barra scura: «CLI», luogo corrente, avatar, fuoco nella barra | red-400 | red-400 | whitelabel; oggi è un letterale #F87171 in 4 punti |
| `--code-bg` / `--code-text` | console | #0B0F17 / #E5E7EB | ink-950 / ink-150 | |
| `--console-err` / `--console-warn` **nuovi** | righe di log | #FF7B7B / #F0C060 | stessi | sostituiscono 5 esadecimali |

**Tolti** (dopo la fetta 3): `--start`, `--stop`, `--restart`, `--light-green`, `--purple` (le varianti colorate dei pulsanti) e `--prod` (la marca produzione diventa neutra con l'icona, §5.10).

### 2.4 Fuoco e campi

```css
--focus-width: 2px; --focus-offset: 2px;
:focus-visible { outline: var(--focus-width) solid var(--focus); outline-offset: var(--focus-offset); }
.topbar { --focus: var(--brand-top); }   /* #B91C1C sulla barra scura farebbe 2,76:1 */
```
- **Un solo anello per tutto**: pulsanti, link, campi, select, `summary`, schede, checkbox. Via `outline:none` e l'alone al 18% (`base.html:276-277`, `login.html`/`invite.html:63-64`, `_new_instance_dialog.html:22`).
- Il fuoco è **diverso dall'hover**: l'hover cambia fondo o sottolineatura, mai l'anello.
- Campo a riposo: bordo `--border-input`; al passaggio: bordo `--muted`; con errore: bordo `--err` (§5.6).

### 2.5 Tipo

```css
--fs-12:12px; --fs-14:14px; --fs-16:16px; --fs-18:18px; --fs-20:20px; --fs-24:24px;
--lh-18:18px; --lh-20:20px; --lh-24:24px; --lh-28:28px; --lh-32:32px;
--fw-regular:400; --fw-semibold:600; --fw-bold:700;
```
Coppie fisse corpo/interlinea (nessun'altra combinazione):

| Ruolo | Corpo/interlinea | Peso | Dove |
|---|---|---|---|
| didascalia | 12/18 | 400 (600 nei badge) | badge, meta, piè di pagina, legende, `code`/`.mono`, console |
| corpo | 14/20 | 400 | tabelle, liste, form, pulsanti (600), schede (600/700) |
| lettura | 16/24 | 400 | `.page-head p`, il primo paragrafo di spiegazione sotto il titolo di un dialogo, il corpo di `help.html` |
| h3 | 16/24 | 600 | titoli di card e di sottosezione |
| h2 | 18/28 | 600 | sezione, titolo di dialogo, `details.group h2` |
| h1 | 24/32 (sotto md: 20/28) | 700 | titolo di pagina; valore dei tile |
| marchio | 18/28 | 700 | `.brand` |
| guida (`help.html`) | h2 20/28, h3 18/28 | 600 | contesto di lettura, un gradino sopra |

Tabelle e liste restano a 14: il 16 «per tutto» del playbook non si copia (§9). Niente `letter-spacing` tranne `-0.01em` su h1 e tile.

### 2.6 Spazi, raggi, contorni, ombre

```css
--sp-2:2px; --sp-4:4px; --sp-6:6px; --sp-8:8px; --sp-10:10px; --sp-12:12px; --sp-16:16px;
--sp-20:20px; --sp-24:24px; --sp-32:32px; --sp-40:40px; --sp-48:48px; --sp-64:64px;
--card-pad: var(--sp-16); --dialog-pad: var(--sp-24);
--r-sm:4px; --r:8px; --r-lg:12px; --r-pill:999px;          /* i cerchi restano 50% */
--bw:1px; --bw-2:2px; --bw-4:4px;                          /* 2: fuoco, indicatore di scheda, scelto; 4: striscia di stato */
--shadow-sm: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.08);  /* card */      scuro: 0 1px 2px rgba(0,0,0,.4)
--shadow-md: 0 4px 12px rgba(16,24,40,.12);                /* menu, campanella, toggletip */ scuro: 0 4px 12px rgba(0,0,0,.5)
--shadow-lg: 0 12px 32px rgba(16,24,40,.16);               /* dialoghi, toast */          scuro: 0 12px 32px rgba(0,0,0,.5)
```
`--shadow` resta come alias di `--shadow-sm` (9 usi).

### 2.7 Controlli e icone

```css
--control-h:36px; --control-h-sm:32px; --icon-btn:32px; --checkbox:18px;
--icon-12:12px; --icon-16:16px; --icon-20:20px;
@media (pointer: coarse) { :root { --control-h:44px; --control-h-sm:44px; --icon-btn:44px; } }
```
Il 44 su schermo tattile è la soglia del canone (Fitts, «bersaglio tattile ≥ 44 px»), decisa qui (D6).

### 2.8 Movimento

```css
--dur-100:100ms; --dur-200:200ms; --dur-300:300ms; --dur-400:400ms;
--ease-out: cubic-bezier(.23,1,.32,1); --ease-in-out: cubic-bezier(.77,0,.175,1);
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration:.01ms !important; animation-iteration-count:1 !important;
                           transition-duration:.01ms !important; scroll-behavior:auto !important; }
}
```
Una regola sola sostituisce le 5 riduzioni sparse e copre `m-pulse`/`m-shine` di `machines.html`, che oggi non si fermano.
- **100**: icona che ruota (chevron), micro-interazioni. **200**: hover, colore, piccoli interruttori. **300**: menu, fold, foglio dal basso. **400**: dialoghi, toast, festa di fine job.
- **Cicli**: nessuna animazione infinita dura più di 5 s di fila (WCAG 2.2.2). Spinner, impulsi, luccichio dello scheletro e omino che corre hanno un numero di giri che sta in 5 s, poi restano fermi nella loro posa; ripartono quando lo stato cambia (un passo nuovo del job, un dato nuovo). Lo dice il testo accanto, non il moto (D3, per Carlo: l'omino).
- Esente: l'interpolazione della barra di avanzamento legata al giro di lettura (`job.html:119`, `width 1.4s linear`): è un dato, non un effetto.

### 2.9 Breakpoint (mobile-first)

```css
--bp-sm:576px; --bp-md:768px; --bp-lg:992px; --bp-xl:1200px;   /* documentazione: le media query non leggono le variabili */
```
Lo stile senza query è quello del telefono; sopra si aggiunge con `@media (min-width: 576px | 768px | 992px | 1200px)`. In `base.html` e nei componenti nuovi (barra, dialoghi, schede, tabelle-card) si scrive così da subito. Nei template di pagina i valori si allineano subito ai quattro e la direzione si gira quando il template si tocca di nuovo (D5): una `max-width` resta ammessa solo come `max-width: 575.98px | 767.98px | 991.98px | 1199.98px`.

---

## 3. Contrasti calcolati (formula WCAG 2.x, come `brand.contrast()`)

| Coppia | Chiaro | Scuro | Soglia | Esito |
|---|---|---|---|---|
| `--text` su `--bg` / `--panel` | 16,41 / 17,74 | 15,65 / — | 4,5 | ok |
| `--muted` su `--bg` / `--panel` / `--panel-2` | **5,34** / 5,78 / 5,53 (era 4,47) | 7,34 / 6,72 / 6,21 | 4,5 | ok |
| `--muted` su `--err-soft` / `--warn-soft` | 5,31 / 5,54 | — | 4,5 | ok |
| `--border-input` su `--panel` / `--bg` / `--panel-2` | **3,30** / 3,05 / 3,16 (era 1,47) | **3,48** / 3,80 / 3,21 (era 1,73) | 3 | ok |
| anello `--focus` su `--panel` / `--bg` / `--panel-2` | **6,47** / 5,98 / 6,19 (era 2,63) | 6,25 / 6,83 / 5,78 | 3 | ok |
| anello nella barra (`--brand-top` su `--side-bg`) | 6,45 | 7,03 | 3 | ok (con `#B91C1C` sarebbe 2,76: per questo lo scope) |
| bianco su `--accent` (primario) | 4,83 | `--on-accent` su red-400: 6,41 | 4,5 | ok |
| bianco su `--danger` / `--danger-hover` | 6,57 / 8,66 | 6,57 / 8,66 | 4,5 | ok |
| `--err` su `--panel` / su `--err-soft` | 6,57 / 6,05 | 6,25 / 6,23 | 4,5 | ok |
| `--ok` su `--ok-soft` | 4,76 | 8,82 | 4,5 | ok |
| `--warn` su `--warn-soft` | 5,20 | 9,51 | 4,5 | ok |
| `--info` su `--info-soft` | 6,16 | 6,68 | 4,5 | ok |
| `--side-text` su `--side-bg` / `--side-hover` | 12,02 / 9,85 | 11,80 / 10,49 | 4,5 | ok |
| `--side-muted` su `--side-bg` / `--side-hover` | **5,80** / 4,75 (era 3,75) | **6,32** (era 4,22) | 4,5 | ok |
| bianco su #B54708 (conteggio della campanella) | 5,43 | 5,43 | 4,5 | ok |
| `--on-selected` su `--selected` | 17,74 | 15,65 | 4,5 | ok |
| `--border` (decorativo) su `--panel` | 1,24 | 1,32 | — | **solo decorativo**: mai l'unico confine di un controllo |

Whitelabel: `brand.check_color()` rifiuta un colore sotto 4,5:1 col bianco; quindi lo stesso colore come anello su `--panel` vale ≥ 4,5 e su `--bg` ≥ 4,1 (sopra il 3 richiesto). Lo scuro prende la tinta che `dark_tint()` porta ad AA. Nessun controllo nuovo in `brand.py`.

---

## 4. Tabelle di sostituzione (valore vecchio → token)

### 4.1 Corpi (321 dichiarazioni + 89 `style=` con un corpo)

| Vecchio | Usi | Nuovo | Eccezione |
|---|---|---|---|
| 13px | 185 | `--fs-14` / `--lh-20` | — |
| 12px | 52 | `--fs-12` / `--lh-18` | — |
| 14px | 26 | `--fs-14` | — |
| 15px | 14 | `--fs-16` | — |
| 11px | 13 | `--fs-12` | testo dentro un SVG di mappa o grafico (§4.7) |
| 16px | 7 | `--fs-16` | — |
| 18px | 5 | `--fs-18` | — |
| 20px | 4 | `--fs-20` | — |
| 12.5px | 3 | `--fs-12` (`code`, `.mono`, `pre.console`, `.addon`) | — |
| 26px / 28px | 3 / 1 | `--fs-24` | — |
| 17px / 19px | 2 / 1 | `--fs-18` / `--fs-20` | — |
| 24px | 2 | `--fs-24` | — |
| 8px | 1 | il segno diventa un'icona da 12 (`_map.html:59`) | — |
| 1rem | 1 | `--fs-16` | — |
| `font: 15px/1.5`, `14px/1.5`, `16px/1.7 mono` | 4 | la coppia del ruolo (§2.5) | — |

Interlinee (9 valori: 1, 1.1, 1.2, 1.3, 1.35, 1.4, 1.6, 0, 18px) → solo le cinque di §2.5; `line-height: 1` e `0` restano ammessi **solo** su elementi senza testo (icone, contatori a pillola). Pesi: 650 → 600 (9), 800 → 700 (3), 500 → 400 (7; nei pulsanti e nelle schede 600).

### 4.2 Spazi fuori scala (padding, margin, gap, inset)

| Vecchio | Usi | Nuovo | Regola per scegliere |
|---|---|---|---|
| 14px | 52 | 12 o 16 | dentro un componente (padding di cella, tile, gap di riga) → 12; tra blocchi o padding di card → 16 |
| 18px | 29 | 16 o 20 | margine sotto un titolo → 16; tra blocchi → 20 |
| 3px | 27 | 2 o 4 | dentro un elemento alto quanto il testo (badge, pillola, tag) → 2; altrimenti 4 |
| 22px | 18 | 24 | — |
| 5px | 13 | 4 o 6 | gap tra icona e testo → 6; il resto → 4 |
| 9px | 12 | 8 o 10 | verticale → 8; orizzontale → 10 |
| 7px / 11px / 13px | 5 / 5 / 2 | 6 / 12 / 12 | — |
| 26px / 28px / 30px | 1 / 4 / 4 | 24 / 24 o 32 / 32 | `main` 28/32 → 24/32 |
| 0, 2, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48 | ~970 | `--sp-*` | il valore resta, diventa token quando la riga si tocca |

Card: `.panel` 20 → `--card-pad` 16; `.server-head` 12/16, `.tile` 12/14, `.machine-top` 10/14 → `--card-pad` 16 (i tile 12, sono dentro una card). Dialogo: 22/24 → `--dialog-pad` 24 (16 sotto sm).

### 4.3 Raggi (22 a mano)

| Vecchio | Nuovo |
|---|---|
| 2px, 3px, 4px | `--r-sm` |
| 6px, 8px, 9px (non pillola), 10px (`.mcard-icon`) | `--r` |
| 12px, 20px (`.m-empty-icon`) | `--r-lg` |
| 99px, 999px, 9px e 10px su un elemento alto ≤ 20 (contatore, `.console-tag`) | `--r-pill` |
| 50% | resta (cerchi) |
| 0 e i quattro raggi composti di segmenti uniti (`_login_as_dialog.html:8-9`, `base.html:192-193`) | 0 / `--r` per angolo |

### 4.4 Contorni e ombre

| Vecchio | Nuovo |
|---|---|
| `3px solid var(--warn/--info/--border/#ff7b7b)` (4) | `--bw-4` (striscia di stato) |
| `border-left-width: 4px` del toast | `--bw-4` |
| `2px solid transparent` delle schede, `2px` del fuoco | `--bw-2` |
| `0 0 0 3px color-mix(…18%…)` (alone di fuoco, 3) e `0 0 0 3px var(--accent-soft)` | tolti: c'è l'anello |
| `0 2px 8px rgba(0,0,0,.12)` (`.chart-tip`) | `--shadow-md` |
| `0 10px 30px rgba(0,0,0,.15)` (`.toast`) | `--shadow-lg` |
| `var(--shadow-lg)` su `.bell-panel`, `.row-menu-items`, `.more-menu` | `--shadow-md` (sono menu, non dialoghi) |
| `var(--shadow)` sui pulsanti | **tolto**: i pulsanti sono piatti |

### 4.5 Durate e curve

| Vecchio | Usi | Nuovo |
|---|---|---|
| .12s, .15s (hover, bordo, colore) | 15 | `--dur-200 var(--ease-out)` |
| .3s, .35s (fold, chevron del fold, step-pop) | 6 | `--dur-300 var(--ease-out)`; chevron → `--dur-100` |
| .4s, .5s (barra machines, toast, job-fade del toast) | 5 | `--dur-400 var(--ease-out)` |
| .8s, .9s (job-fade, job-thumb, job-danger: una volta) | 3 | `--dur-400 var(--ease-out)` |
| `ease`, `ease-out`, `ease-in-out` scritti a mano | — | `--ease-out` (entrate, aperture); `--ease-in-out` (avanti e indietro: indicatore di scheda, interruttori) |
| periodi di ciclo (.2-.6s, .8s, 1.2s, 1.4s, 1.6s) | 17 | restano come periodo; il **numero di giri** porta il ciclo sotto i 5 s (§2.8) |
| `width 1.4s linear` della barra del job | 1 | resta (dato) |

### 4.6 Media query (37)

| Vecchia | Usi | Nuova |
|---|---|---|
| `max-width: 560px` | 1 | sm 576 |
| `max-width: 600px` | 6 | sm 576; **la barra in alto → md 768** (§5.11) |
| `max-width: 640px` | 6 | **md 768** (passi del job, `server.html:986-999`, `enterprise.html`, mappa: il layout da telefono un po' più largo non rompe; quello da desktop più stretto sì) |
| `max-width: 700px` | 13 | md 768 |
| `max-width: 760px` (login, invito) | 4 | md 768 |
| `max-width: 900px` | 2 | lg 992 |
| `max-width: 1000px` | 1 | lg 992 |
| `max-width: 1279px` | 1 | xl 1200 |
| `min-width: 701px` | 1 | md 768 |
| `min-width: 1100px` | 1 | xl 1200 |
| `prefers-reduced-motion` | 5 | una sola, nei token (§2.8) |
| `prefers-color-scheme: dark` | 3 | una sola, nei token (§2.1) |

### 4.7 Fuori dalle scale (dichiarato, non dimenticato)

Il testo dentro un SVG che si disegna con coordinate (`.map-canvas text`, `.map-flow text`, `.chart-axis`, `.chart-capacity-label`, `svg.spark`) scala col `viewBox` e non è un corpo di pagina: resta com'è e non conta nei controlli §11. I colori `--cat-*` e `--series-*` idem.

---

## 5. Componenti

### 5.1 Pulsanti: quattro varianti

| Variante | Classe | Aspetto | Quando |
|---|---|---|---|
| **primario** | `.primary` | fondo `--accent`, testo `--on-accent`, hover `--accent-hover` | l'azione che crea della scheda o della lista (UX-006 §3, decisione del 21/11). **Al più uno visibile per vista** |
| **secondario** | nessuna classe (il default) | fondo `--panel`, bordo `--border-strong`, testo `--text`, hover `--panel-2` | tutto il resto |
| **terziario** | `.ghost` (la classe resta: 5 usi) | trasparente, senza bordo, testo `--text`, hover `--panel-2` | azioni di bassa enfasi, la X, i pulsanti-icona, i pulsanti della barra |
| **pericolo** | `.danger` | fondo `--panel`, bordo `color-mix(--err 45%, --border)`, testo `--err`; hover `--err-soft` | ciò che ferma, cancella, sostituisce |
| pericolo confermato | `.primary.danger` | fondo `--danger`, testo `--on-danger`, hover `--danger-hover` | **solo** il pulsante «vai» della conferma di un'azione che non si annulla (omniaAsk, omniaTypeName, `data-confirm-danger`) |

Misure: `min-height: var(--control-h)` (36; 44 al tocco), `.small` → `var(--control-h-sm)` (32; 44 al tocco), padding orizzontale 12 (small 10), corpo 14/20 peso 600, raggio `--r`, icona 16 con gap 6, nessuna ombra. Disabilitato: `opacity: .45`, `cursor: not-allowed`, i link con `aria-disabled="true"`.

`.icon-btn`: `var(--icon-btn)` × `var(--icon-btn)` (32; 44 al tocco), padding 0, centrato, terziario per default, icona 16 (20 nella barra e per la X). **Obbligatori** `aria-label` (il nome corto) e `title` (l'aiuto: cosa fa).

Le varianti di oggi:

| Oggi | Dove | Diventa |
|---|---|---|
| `btn-start` (Avvia) | 2 | secondario + icona `play` |
| `btn-stop` (Ferma istanza, Ferma job) | 3 (`job.html:54` è già `.danger`) | pericolo + icona `square` |
| `btn-restart` (Riavvia) | 2 | secondario + icona `rotate-cw` |
| `btn-test` (Prova) | 3 (`config.html:37`) | secondario |
| `btn-activate` (Prova e attiva) | 3 (`config.html:37`) | secondario: è un'azione di riga, ce ne sono N; il primario della pagina resta «Nuovo server» |
| `.button.btn-edit` (Modifica) | 3 (`config.html:38`) | secondario |
| `.button.btn-clone` (Clona) | 3 (`config.html:39`) | terziario |
| `.button.current` | 0 usi | **CSS morto, si toglie** (`base.html:500`) |
| `.copy-button`, `.toast-close`, `.fold-toggle`, `.olog-copy` | 4 | terziario (`.toast-close` → `.icon-btn` con la X) |
| `.console-tag` (pillole con `aria-pressed`) | — | terziario `.small` pillola; premuto = `--selected`/`--on-selected` (oggi rosso) |
| `.side-tools` (lingua, tema, Esci nella barra) | 3 | terziario coi token della barra (`--side-*`); il tema diventa `.icon-btn` `moon`/`sun` |
| `.bell-button` | 1 | `.icon-btn` `bell` 20 |
| `.menu-toggle` | 1 | terziario con icona `menu` 20 **e** la parola «Menu» |

### 5.2 Rosso solo per primario e pericolo (cambio 2)

Il rosso (`--accent`, `--accent-strong`, `--danger`, `--err`) compare solo in: il pulsante primario; i pulsanti di pericolo; l'anello di fuoco; i marchi sulla barra scura (`--brand-top`: «CLI», luogo corrente, avatar); gli stati d'errore (`--err` con icona o parola, §5.8). **Altrove niente.**

- `a`: `color: var(--link)` (= `--text`). **Link nella prosa** (paragrafi, `dd`, guida, banner): sottolineati sempre (`text-underline-offset: 2px`, spessore 1px; hover 2px). **Link di riga e nomi** (celle di tabella, `.row-name`, nome dell'istanza, `h3 a` delle card, «Sulla mappa», «tutte le azioni»): `--text`, nome dell'istanza peso 600, **sottolineato al passaggio e al fuoco**. Briciole: `--muted`, la corrente `--text`, sottolineate al passaggio.
- I 55 usi di `var(--accent)` fuori dal primario: `base.html:137` (`.link-bar`) → fondo `--side-hover`, testo `--side-text`, icona `link`; `:183`/`:546` (hover dei link nella testata e nella campanella) → sottolineatura; `:187`, `:500` → CSS morto, via; `:258` (tag premuto) → `--selected`; `:262` (area di rilascio della console) → contorno tratteggiato `--info`; `:304`, `index.html:22`, `server.html:59` (scheda attiva) → `--selected` (§5.3); `:337` (hover del fold) → sottolineatura; `index.html:34` (periodo premuto) → `--selected`; `server.html:47` (menu Altro aperto) → `--border-input`; `settings.html:13` (card al passaggio) → `--border-input`; `machines.html:22` → sottolineatura; `:41` (spark), `:53`/`:56` (avanzamento), `:86` (passo fatto) → `--series-1`; `:72` (icona del vuoto) → `--muted` su `--panel-2`; `:101`, `_new_instance_dialog.html:20-21` (card scelta) → bordo `--bw-2` `--selected` + icona `check`; `_login_as_dialog.html:10` (segmento scelto) → `--selected`/`--on-selected`; `_map.html:129` (`.map-more`) → `--text` sottolineato. Restano: `base.html:227-229` (primario), `login.html`/`invite.html:65-67`, `:84` (primario), `login.html:54` (`h1 span`, marchio).
- **Marca produzione** (`.role.production`, `--prod`): testo `--text` peso 600 con l'icona `rocket`; la staging `--info` con `flask-conical`. Il significato sta nella parola e nell'icona (D4, decisa qui).

### 5.3 Schede (cambio 8)

- Markup che c'è già: `role="tablist"` (7: `instances.html:50`, `index.html:109`, `pullers.html:76`, `users.html:41`, `workspace.html:73`, `server.html:358`, `_login_as_dialog.html:18`), le schede `role="tab"` con `aria-selected` e `aria-controls`; i pannelli `role="tabpanel"` con `aria-labelledby` verso la scheda e `tabindex="0"`.
- **Uno script comune in `base.html`** per ogni `[role="tablist"]`: ← → spostano il fuoco (in giro), Home/End alla prima e all'ultima; **tabindex a rotazione** (la scelta 0, le altre −1). **Attivazione manuale**: Invio/Spazio (il click del pulsante, che ogni pagina già gestisce). Il motivo: le schede Database, Moduli e Backup leggono dal server; con l'attivazione automatica scorrere le schede lancerebbe una lettura per ognuna.
- Aspetto: inattiva peso 600 `--muted`; **attiva peso 700 `--text`**, indicatore `--bw-2` in `--selected` (non rosso); hover `--text`; padding 8/12. Via i duplicati di `index.html:22` e `server.html:59`.
- Barra che scorre: sfumatura sul bordo che ha altro da vedere (`mask-image` a gradiente di 24px, solo quando `scrollWidth > clientWidth`), così si capisce che continua.

### 5.4 Tabelle (65 tabelle, 267 `th`)

- `scope="col"` su ogni `th` di `thead`; `scope="row"` su un `th` nel corpo.
- Nome accessibile: `aria-labelledby` verso il titolo della sua sezione (con un `id`); `aria-label` solo dove la tabella non ha titolo. **Nessuna `<caption>` visibile**: ripeterebbe il titolo (smell «dato duplicato»).
- Intestazioni: **iniziale maiuscola** (via `text-transform` e `letter-spacing`), 14/20 peso 600, `--muted` su `--panel-2`.
- Celle: padding 8/12 (era 9/12), corpo 14/20; numeri a destra con `tabular-nums`.
- Il nome di riga è un link (§5.2); la riga cliccabile (`tr.click-row`) resta, il link è il bersaglio della tastiera.
- Sotto sm le liste quotidiane (Backup, Istanze) diventano card una riga sopra l'altra (lo schema di `backups.html:32` oggi); le altre scorrono dentro la loro card (`.scroll`), mai la pagina.

### 5.5 Dialoghi (53 nei template + quello di `omniaAsk`)

- **La X**: macro `dialog_x()` in `_icons.html`, **ultimo figlio** del `<dialog>` (così il primo fuoco di `showModal()` resta quello di oggi), posizionata in alto a destra (`position:absolute; top/right: var(--sp-12)`); il titolo prende `padding-right` 40. È un `.icon-btn` con l'icona `x` 20, `aria-label="{{ _('Close') }}"`, `title="{{ _('Closes without changing anything') }}"` (stringhe che esistono già).
- **Fa quello che fa Esc**: un gestore comune in `base.html` manda al dialogo un evento `cancel` annullabile; se nessuno lo annulla, `dialog.close()`. I dialoghi che oggi bloccano Esc mentre un caricamento gira bloccano anche la X, senza codice in più. `omniaAsk`/`omniaSay`/`omniaWrite`/`omniaTypeName` la ricevono nel loro HTML generato: X = No / Annulla / Chiudi.
- `aria-labelledby` verso il titolo in tutti: mancano in `_checks_dialog.html`, `_backup_dialog.html`, `_from_link_dialog.html`, `_pull_dialog.html`, `_actions.html`, `destinations.html`, `_login_as_dialog.html`, `_restore_dialog.html` (12 dialoghi secondo l'audit). `omniaAsk`: `aria-labelledby` sul suo paragrafo.
- **Foglio dal basso sotto sm** (lo stile senza query): `inset: auto 0 0 0`, larghezza 100%, `max-height: 90dvh`, raggio `--r-lg --r-lg 0 0`, contenuto che scorre, riga delle azioni attaccata in fondo, padding 16. **Da sm in su**: centrato, `width: min(640px, 100vw - 32px)`, padding 24. Entrata: foglio `translateY` `--dur-300 --ease-out`; centrato dissolvenza `--dur-200`.
- Riga delle azioni (`.form-actions`, una classe per tutti): in fila con gap 8 da sm in su; sotto sm in colonna, ogni pulsante largo quanto il foglio. L'ordine dei pulsanti resta quello di oggi (il «vai» prima, «Annulla» dopo): Jakob dentro il pannello. Così «Aggiorna moduli» smette di impilare su desktop.
- Titolo del dialogo h2 18/28; il primo paragrafo di spiegazione 16/24 (§2.5).

### 5.6 Campi

- Ogni campo ha un nome: `<label for>` o il campo dentro `<label>` (285 label, 166 con `for`: le altre 119 devono contenere il campo). Il `placeholder` non è mai l'unica etichetta (101: si controllano).
- **Suggerimento** sotto il campo (`.hint`, `--muted` 12/18 o 14/20): con un `id`, e il campo con `aria-describedby` verso di lui (oggi 3).
- **Errore** di un campo (controllo nel browser o rifiuto del server che nomina il campo): un `.field-error` con `id`, icona `circle-x` 16 e testo `--err`; il campo prende `aria-invalid="true"` e `aria-describedby` con l'id dell'errore (in coda al suggerimento); il bordo diventa `--err`. All'invio, il fuoco va al primo campo con errore. Al primo `input` sul campo, `aria-invalid` e il messaggio vanno via.
- Altezza `var(--control-h)`; padding 6/10; bordo `--border-input`; disabilitato: fondo `--panel-2`, testo `--fg-disabled`.
- Checkbox e radio: `var(--checkbox)` 18×18, `accent-color: var(--selected)` (neutro, non rosso); allineati alla prima riga dell'etichetta; tutta l'etichetta è il bersaglio (≥ 24).

### 5.7 Badge

`.badge`: 12/18 peso 600, padding 2/8, raggio `--r-pill`. **Icona 12** prima della parola su `ok` (`check`), `warn` (`triangle-alert`), `error` (`circle-x`); `info`, `test`, neutro: senza icona. L'icona è disegnata dal CSS (`::before` con `mask-image` e `background-color: currentColor`, il percorso Lucide in un `data:` URI), così **nessuno dei 51 badge nei template e degli 84 creati in JS cambia markup**. È decorativa: la parola porta il significato.

### 5.8 Banner e avvisi

`.banner` (errore di default), `.info`, `.ok`, `.warn`: padding 12/16, raggio `--r`, bordo 1px del colore di stato, fondo `-soft`, **testo `--text`**, **icona 20** al principio dello stesso colore del bordo (`circle-x`, `info`, `circle-check`, `triangle-alert`), sempre via `::before` (48 banner: 0 markup). Un banner che compare dopo un'azione: `role="alert"` (errore, avviso) o `role="status"` (riuscito). I paragrafi arancioni sciolti (~25, l'audit li ha visti a schermo) diventano `.banner.warn`.

### 5.9 Icone

**Sorgente**: Lucide (licenza ISC; le parti nate da Feather, MIT), solo i simboli che servono, **copiati nel repo** come `<symbol>` in un partial `_sprite.html`, incluso una volta in `base.html` (e in `login.html`/`invite.html` se usano icone) subito dopo `<body>`, dentro un `<svg width="0" height="0" aria-hidden="true" style="position:absolute">`. Nessun CDN, nessun font di icone, niente a runtime. In testa al partial le due note di licenza (ISC di Lucide, MIT di Feather), e una riga nella pagina Informazioni fra i componenti di terzi.

**Uso**: macro `icon(name, size=16, label=None)` in `_icons.html` → `<svg class="i i-16" aria-hidden="true" focusable="false"><use href="#i-<name>"/></svg>`; con `label`: `role="img"` e `aria-label` (solo dove l'icona è l'unico portatore di significato). In JS un aiutante `window.omniaIcon(name, size)` crea lo stesso nodo. CSS: `.i { width/height: var(--icon-16); stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: none; }`, `.i-12`, `.i-20`. **Tre misure**: 12 (solo nei badge), 16 (accanto al testo di 14), 20 (barra, X, banner).

| Oggi | Usi | Simbolo | Misura | Note |
|---|---|---|---|---|
| ✓ | 28 | `check` (in un esito: `circle-check`) | 16 | |
| ✗, ✕ come esito | 26 | `circle-x` | 16 | |
| ✕, × come chiusura | ~4 | `x` | 20 | in un `.icon-btn` con `aria-label` |
| ⚠ | 17 | `triangle-alert` | 16 | |
| ⛔ | 2 | `octagon-alert` | 16 | campanella, critico |
| ↗ | 20 | `external-link` | 16 | il link ha già la parola; l'unico solo-icona (`server.html:721`) prende `aria-label` |
| ▾ | 12 | `chevron-down` | 16 | ruota di 180° quando è aperto, `--dur-100` |
| › delle briciole (`base.html:505`, CSS) | 1 | `chevron-right` | 16 | nel macro di `_crumbs.html` |
| `.fold-icon` | 6 | `chevron-down` | 16 | −90° chiuso |
| 🔔 | 1 | `bell` | 20 | |
| ☰ | 1 | `menu` | 20 | con la parola «Menu» |
| ☾ / ☀ | 2 | `moon` / `sun` | 20 | |
| ⟳ (in corso) | 6 | `loader-circle` che gira (≤ 5 s) | 16 | |
| ↻ | 1 | `rotate-cw` | 16 | |
| ■ ○ ● fuori dalla mappa (`workspaces.html:471-477`) | ~5 | il pallino `.state-dot` che c'è già | — | |
| ⏳ ⋯ ← ↑ | 4 | `hourglass`, `ellipsis`, `arrow-left`, `arrow-up` | 16 | |
| «+ » in testa a un'etichetta | 8 | `plus` | 16 | |
| Copia / Copiato ✓ | 1 | `copy` / `check` | 16 | |
| Esci nel ☰ | — | `log-out` | 16 | |
| `_icons.html`: start, stop, restart, backup, download, production, test, terminal | 8 | `play`, `square`, `rotate-cw`, `database`, `download`, `rocket`, `flask-conical`, `terminal` | 16 | |
| `_icons.html`: github, python | 2 | restano i loro percorsi (marchi: Lucide non li ha) | 16 | |
| server in `machines.html:134`, `:300` | 2 | `server` | 20 (lista); nel vuoto resta un disegno da 40, fuori dalla scala come l'omino | |
| omino che corre, pollice, segnale di pericolo | — | **restano** illustrazioni dei job (non sono icone) | — | rispettano §2.8 |

**Restano testo**: › e → nella prosa e nei percorsi a menu tradotti («Impostazioni › Destinazioni», «{from} → {to}»: circa 105 + 20), i glifi nei testi che scrive il server (4 in Python, le righe dei job) e i segni dentro l'SVG della mappa (D2).

**Glifi dentro le stringhe da tradurre**: 28 chiamate `_("✗ …")`, `_("⚠ …")`, `_("✓ …")` nei template e 56 voci in `omniacli/i18n/it/*.json`. Il glifo esce dal msgid; l'icona la mette il template o lo script accanto alla stringa; l'italiano si aggiorna nella stessa fetta, a ciclo chiuso.

### 5.10 Menu

- **CSS morto, via**: `.menu`, `.menu-label`, `.menu-items` (`base.html:184-193`, 0 usi), `.button.current` (`:500`).
- I menu a `details` (Altro ▾ di `server.html:232`, «Azioni ▾» di `backups.html:310`, «Apri ↗ ▾» di `_environments.html:24`/`:419`): `summary` con `chevron-down` che ruota; pannello `--shadow-md`, raggio `--r-lg`, padding 4; voci terziarie alte `--control-h`, testo a sinistra; le voci di pericolo `.danger`. **Un gestore comune** in `base.html` per `details.menu`: Esc chiude e rimette il fuoco sul `summary`, un click fuori chiude (oggi solo `backups.html:406`).
- I menu a pulsante (campanella, ☰) hanno già `aria-expanded`; restano.
- Le 13 voci di «Altro» e lo spostamento di Console e «Sostituisci il database» nella pagina: **non sono di questo contratto** (IA della pagina istanza, UX-006).

### 5.11 La barra in alto sul telefono (cambio 5)

| Larghezza | Nella barra | Nel ☰ |
|---|---|---|
| < md (768) | marchio (solo logo), ☰ Menu, campanella, tema | i luoghi; poi un separatore; il tuo account (avatar e nome); la lingua (EN \| IT, `aria-current`); **Esci** (terziario largo con `log-out`) |
| md – lg | marchio (solo logo), luoghi, campanella, avatar, lingua, tema, Esci | — |
| ≥ lg (992) | come sopra, col nome del marchio e il nome dell'utente | — |

Niente sparisce senza un posto: oggi sotto 560 «Esci» e la lingua non ci sono da nessuna parte (`base.html:143-145`). Esc chiude il ☰ e rimette il fuoco sul pulsante.

**Backup a 400**: il menu di riga sotto sm è un `.icon-btn` `ellipsis` con `aria-label="{{ _('Actions') }}"` e `title` di oggi; la prima cella prende `padding-right` = `--icon-btn` + 8, così il badge «Staging» non finisce sotto il pulsante (oggi «Azioni ▾» lo copre).

### 5.12 Scheletri

- `.skeleton-line` (alta quanto l'interlinea del testo che sostituisce, 20 di default; larghezze `.w-40` `.w-60` `.w-80`) e `.skeleton-block` (`height: var(--h, 64px)`): fondo `--panel-2`, raggio `--r-sm`; impulso d'opacità 1 → .6 su `--dur-400`, al più 5 s (§2.8).
- Il contenitore ha `aria-busy="true"` e un testo `role="status"` per i lettori di schermo («Lettura dei database…», il testo di oggi). **Dopo 5 s** il testo diventa visibile accanto allo scheletro: chi aspetta sa che il pannello lavora ancora (Nielsen 1).
- Dove: Database della macchina (`_machine_database.html:129`), Moduli (`_modules_tab.html:19`), Backup (lista globale `backups.html:64` e scheda Backup dell'istanza). Gli scheletri fatti a mano di oggi (`.deploy-card.skeleton`, `.m-skeleton`, `.map-skeleton`) passano alle stesse classi. Gli altri «Lettura…» restano testo.

### 5.13 Aiuti al passaggio (`title`): la regola

Decisa qui, piccola e sicura:

1. **Il contenuto dei 773 `title` resta.** È il canone («ogni pulsante ha il suo aiuto») e `test_the_page_explains_itself` lo controlla.
2. **Campi** (60 `title` su `input`/`select`/`textarea`): se il testo dice cosa scrivere o cosa cambia, diventa il `.hint` visibile sotto il campo (con `aria-describedby`) e il `title` del campo va via (non lo stesso testo due volte). Se un suggerimento visibile lo dice già, il `title` va via e basta.
3. **Pericolo** (70 pulsanti `.danger`): la conseguenza scritta nel `title` deve essere anche nel testo della conferma (`data-confirm`, `omniaAsk`, `omniaTypeName`), che sul telefono è l'unico posto dove si legge.
4. **Pulsanti-icona**: `aria-label` col nome corto, `title` con l'aiuto.
5. **«?» toggletip**: solo dove una scelta si fa leggendo una spiegazione più lunga di un suggerimento, e chi la fa può essere sul telefono. Al più **uno per sezione**. Un `.icon-btn` `circle-help` 16 (bersaglio 32/44) con `aria-label="{{ _('Help') }}: <sezione>"` e `aria-expanded`, che apre sotto di sé un riquadro (`--shadow-md`, max 320px) col testo del `title`, annunciato con `role="status"`; Esc e un click fuori lo chiudono. **Nessun posto obbligatorio in questa build**: i candidati sono i `summary` con `title` delle Opzioni e delle Impostazioni (52 `summary` con `title` in tutto), da scegliere schermo per schermo con il loro contratto.
6. **Niente** libreria di tooltip né un tooltip al fuoco per tutti i 773: duplicherebbe il `title`, che i lettori di schermo già leggono come descrizione.

---

## 6. Whitelabel (`brand.py`)

Un whitelabel può sovrascrivere **solo**: `--accent`, `--accent-soft`, `--on-accent`, `--accent-strong` (fuoco), `--brand-top`, `--brand-on-accent` (chiaro e scuro, come oggi), più nome e logo. `--accent-hover` lo segue da solo (`color-mix`).

Non può toccare: `--danger*` e `--err/--ok/--warn/--info` (il pericolo resta rosso anche con un marchio blu: per questo il fondo del pericolo è un token suo), i neutri, `--link` (ora neutro), `--selected`, tipo, spazi, raggi, ombre, movimento.

Cosa cambia in `brand.css()`: **non emette più `--link`**; emette `--accent-strong: <colore>` nel chiaro e `--accent-strong: <tinta>` nello scuro; i letterali `#111827` e `#fff` che emette restano: sono il testo sopra il suo colore, scelto per il contrasto. Le regole che emette per `.nav-link[aria-current]`, `.avatar`, `.brand-name span` restano. Login e invito ricevono lo stesso `brand.css` di oggi e, con `_tokens.html`, gli stessi token.

Prova: con `#0B5FFF` il primario è blu, l'anello blu, il pericolo `#B42318`, i link e i nomi neutri; nessun altro elemento blu (§11 A7, A15).

---

## 7. Il necessario (Nielsen 1·3·5·9·10)

- **1 Stato**: lo stato porta sempre parola + icona + colore (badge, banner, pallino con parola), mai il solo colore; lo scheletro dice cosa legge, e dopo 5 s lo scrive; il cambio di tema e di lingua si vede subito.
- **3 Annulla**: la X in ogni dialogo fa quello che fa Esc e Annulla; nessuna X su un'azione già partita la ferma (ferma solo il dialogo, quando il dialogo lo permette).
- **5 Prevenzione**: pericolo distinguibile dal primario anche in whitelabel (rosso fisso); la conferma del pericolo dice la conseguenza (§5.13 punto 3).
- **9 Errore**: errore del campo accanto al campo, con `aria-invalid` e il rimedio nel testo; banner d'errore con icona e `role="alert"`.
- **10 Aiuto**: i `title` restano; sul telefono l'essenziale è nei suggerimenti visibili e nelle conferme.

---

## 8. HIDE / DEFER

| Cosa non si fa adesso | Rete |
|---|---|
| Rinominare i token semantici | i nomi di oggi restano; i nuovi sono pochi e additivi |
| Girare a mobile-first le 37 query dei template di pagina | valori allineati subito; direzione girata quando il template si tocca (D5) |
| Icone nell'SVG della mappa | restano segni di testo (D2) |
| IA del menu «Altro» (13 voci) | UX-006 |
| Toggletip obbligatori | regola pronta, posti scelti coi contratti degli schermi |
| `light-dark()` | Jinja `set` (D1) |
| Scheletri dove la lettura è breve | restano «Lettura…» |

---

## 9. Cosa non si copia da Toyota

Logo, font Toyota Type e Nobel, palette teal/copper/rosso Toyota; superfici EV/bz; raggi 0 e la scala 3/5/7/10/15; pulsanti 50/38/32 e l'accent button, AI button, social button; titoli giganti; `62.5%` e il divieto di `rem`; carosello, model card, energy label; icone di settore (auto, Kinto); breakpoint 2xl 1600 e il riallineamento 390/1024/1440/1920; **il 16 per tutto** (tabelle e liste restano 14); la durata 600 ms (le nostre transizioni non sono «drammatiche»).

Si prende la regola, non il marchio: primitivi separati dai semantici, livelli emphasis/default/subtle/muted (muted solo disabilitato), tabella delle coppie testo/superficie, scale chiuse, tre livelli di pulsante più il pericolo, WCAG per componente.

---

## 10. Ordine di lavoro: fette che si rilasciano da sole

Prima di tutto, **fetta 0** (nessun cambio al prodotto): si girano le misure di oggi e si tengono come base (`uxcheck.py`, `uxpages.py` in `/home/carlo/omniacli-local/`, più lo script dei conteggi di §11 su lab3), chiaro e scuro, 1440 e 400. Ogni fetta dopo si chiude con: `pytest` verde, i conteggi di §11 che la riguardano, le schermate prima/dopo degli schermi toccati, chiaro e scuro, e con un marchio `#0B5FFF` dove tocca i colori.

| # | Fetta | Cambio | Posti attesi | Cosa si prova |
|---|---|---|---|---|
| 1 | `_tokens.html` (primitivi, semantici, scuro una volta), login e invito sullo stesso partial, fuoco unico, `--border-input`, `--muted` e `--side-muted` nuovi, `brand.py` (`--accent-strong` al posto di `--link`) | 1, 9 | ~45 in 6 file (`base.html`, `login.html`, `invite.html`, `_new_instance_dialog.html`, `brand.py`, `test_whitelabel.py`) | A1, A2, A9 (esadecimali), A15 |
| 2 | Rosso solo per primario e pericolo: `a`, link di riga e nomi, briciole, i ~25 usi di `--accent` di §5.2, `.link-bar`, marca produzione | 2 | ~35 in 10 file | A7, A1 |
| 3 | Pulsanti: quattro varianti, altezze, `.icon-btn`, `pointer: coarse`, via `--start…--purple` e `.button.current` | 4 | ~60 (base + `config.html`, `job.html`, `options.html`, `server.html`, `_map.html`) | A4, primari ≤ 1 per vista |
| 4 | Barra sul telefono (Esci, lingua, account nel ☰, md 768) e menu di riga dei Backup | 5 | 4 | A13, A6, sovrapposizione badge/pulsante = 0 |
| 5 | Dialoghi: `dialog_x()`, gestore X = Esc, `aria-labelledby` mancanti, foglio dal basso, `.form-actions` | 6 | 53 + 1 (`omniaAsk`) + 12 + 2 regole | A5, A6 |
| 6 | Schede: script comune, attiva 700, indicatore neutro, sfumatura, via i duplicati | 8 | ~12 (base + `index.html`, `server.html`, attributi dei pannelli nei 7 tablist) | A10 |
| 7 | Icone: `_sprite.html`, macro, `omniaIcon`, glifi → simboli, glifi fuori dalle 28 stringhe e dalle 56 voci `it.json`, badge e banner con l'icona, ~25 paragrafi → `.banner.warn` | 7 | ~200 (≈150 glifi nei template, 28 msgid, 56 voci, 2 regole CSS, ~25 paragrafi) + `test_addresses.py:333` | A8, `test_the_page_explains_itself` |
| 8 | Scala dei caratteri, interlinee, pesi; testo da leggere a 16 | 3 | ~330 corpi + 19 pesi + 13 interlinee | A3, A6 (il 13→14 allarga le tabelle: si riguarda 400 e 1440) |
| 9 | Scale di spazi, raggi, contorni, ombre, durate, media query, riduzione del moto unica, cicli ≤ 5 s | 10 | ~280 (≈165 spazi, 22 raggi, 7 contorni/ombre, 30 durate, 37 query, 17 cicli) | A9, A14, A6 a 375/400/575/767/991 |
| 10 | Tabelle: `scope`, nome accessibile, intestazioni a iniziale maiuscola, padding | — | 267 `th` + 65 tabelle + 1 regola | A11 |
| 11 | Campi e aiuti: `aria-describedby`, `aria-invalid` e `.field-error`, checkbox 18, i 60 `title` dei campi, conferme dei 70 pericoli (§5.13) | — | ~60 campi + ~10 conferme da completare + 2 regole | A12 |
| 12 | Menu (CSS morto via, chevron che ruota, gestore comune) e scheletri | — | 10 righe via + 4 menu + 6 scheletri | A14, A6 |

Le fette 1-6 cambiano poco codice e molto aspetto: si rilasciano una alla volta. Le 7-9 sono meccaniche e larghe: una fetta, un commit, una revisione delle schermate di tutti gli schermi. Nessuna fetta lascia un template a metà fra due sistemi: un file si converte intero dentro la sua fetta.

---

## 11. Criteri di accettazione (conteggi)

Si misura con Playwright su lab3 come **due persone reali**: P1 admin della macchina e P2 admin del workspace `acme` (`scoped`), mai con un account di prova che vede più del reale. Schermi: i 12 dell'audit + 4 dialoghi (staging, nuova istanza, aggiorna moduli, carica) + login + invito; 1440×900 e 400×800 (A6 anche 375, 575, 767, 991); chiaro e scuro; una passata con il marchio `#0B5FFF`; una con `prefers-reduced-motion: reduce`; una con `hasTouch`/`isMobile`.

| # | Controllo | Soglia |
|---|---|---|
| A1 | Contrasto del testo visibile (colore calcolato contro lo sfondo effettivo; esclusi i disabilitati) | **0** sotto 4,5:1 (3:1 per ≥ 24px o ≥ 18,66px grassetto); le coppie di §3 calcolate come dette |
| A2 | Fuoco: Tab su ogni elemento interattivo; contorno ≥ 2px, colore ≥ 3:1 sullo sfondo, diverso dallo stile di hover | **0** anelli sotto 3:1; **0** `outline: none` senza sostituto |
| A3 | Corpi distinti del testo visibile per schermo (escluso il testo dentro gli SVG di §4.7) | **≤ 6**, tutti in {12, 14, 16, 18, 20, 24}; pesi in {400, 600, 700}; interlinee in {18, 20, 24, 28, 32} |
| A4 | Bersagli: `button`, `a.button`, `summary`, `[role=tab]`, `input`, `select`, `.icon-btn` (esclusi i link dentro una frase) | **0** sotto 24×24; pulsanti alti 32 o 36 col mouse, **44** al tocco; primari visibili **≤ 1** per vista |
| A5 | Dialoghi | **54/54** con una `.dialog-x` che ha `aria-label` e `title`; **54/54** con `aria-labelledby` verso un titolo visibile; la X chiude senza richieste al server e come Esc; a 400 il dialogo tocca il fondo (±1px) ed è largo quanto la finestra |
| A6 | Scorrimento orizzontale della pagina | **0** schermi con `scrollWidth > clientWidth`, a ogni larghezza, chiaro e scuro |
| A7 | Rosso: elementi visibili con colore, fondo o bordo uguale a `--accent`, `--accent-hover` o `--accent-strong` calcolati | **0** fuori da: primario, pericolo, anello di fuoco, marchi della barra; link di riga e nomi delle istanze con colore = `--text` |
| A8 | Icone | **0** dei glifi di §5.9 nel testo visibile (esclusi › e → della prosa e la mappa); **ogni** `svg > use` decorativo con `aria-hidden="true"`; misure in {12, 16, 20} |
| A9 | Statico sui template (grep) | **0** esadecimali fuori da `_tokens.html` e `_sprite.html` (ammessi i `--cat-*` e i percorsi di github/python); **0** `font-size` in px fuori dai token (salvo §4.7); **0** atomi di spazio in {3, 5, 7, 9, 11, 13, 14, 18, 22, 26, 28, 30}px in padding, margin, gap; raggi solo token, 0 o 50%; durate solo `--dur-*`; media query solo sui quattro valori; **1** regola `prefers-reduced-motion` |
| A10 | Schede | sui 7 tablist: → ← Home End spostano il fuoco; **1** sola scheda con `tabindex="0"`; Invio attiva; attiva peso 700 |
| A11 | Tabelle | **267/267** `th` con `scope`; **65/65** tabelle con nome accessibile; **0** `th` in maiuscolo |
| A12 | Campi | ogni campo con un suggerimento ha `aria-describedby`; errore provocato in 3 form (nome della nuova istanza, e-mail di un utente, destinazione): `aria-invalid="true"`, messaggio letto, fuoco sul campo; checkbox **18×18** |
| A13 | Barra a 400 | Esci, lingua, account raggiungibili dal ☰: **3/3**; a 1440 visibili nella barra |
| A14 | Moto | a t = 6 s da un'apertura, **0** animazioni infinite ancora in corsa (`document.getAnimations()`), salvo quelle ripartite per un cambio di stato negli ultimi 5 s; con `reduce`, **0** animazioni e transizioni oltre 0,01 ms (salvo l'avanzamento del job) |
| A15 | Whitelabel `#0B5FFF` | primario e anello = il marchio; pericolo = `#B42318`; A1, A2, A7 passano chiaro e scuro, login e invito compresi |
| A16 | Test | `pytest` verde; `test_whitelabel.py` cambiato solo dove cambia la stringa (`--link` → `--accent-strong`); `test_addresses.py:333` sull'icona al posto di ↗ |

**Smell che fanno FAIL** (oltre a quelli del canone):
- un nome d'istanza o un link di riga rosso; una scheda attiva, un segmento scelto o una casella spuntata in rosso;
- un pulsante pericoloso che non si distingue dal primario (stesso fondo, stessa parola) o un pericolo in colore di marchio;
- un `outline: none` senza anello; un campo col bordo sotto 3:1;
- un colore, un corpo, uno spazio, un raggio o una durata scritto a mano in un template;
- un glifo o un'emoji usato come icona; un'icona senza `aria-hidden` che ripete la parola accanto; un'icona sola senza nome;
- un dialogo senza X o con una X che manda qualcosa al server;
- un'animazione che gira oltre 5 s o che non si ferma con la riduzione del moto;
- uno stato detto dal solo colore;
- una cosa della barra (Esci, lingua) che a una larghezza non è da nessuna parte;
- uno schermo che scorre in orizzontale; un `title` tolto da un pulsante;
- schermate di accettazione prese con un account che vede più della persona reale.

---

## 12. Decisioni

Prese qui (non chiedono Carlo):
- **D1** scuro scritto una volta con un `set` Jinja, non con `light-dark()` (§2.1).
- **D4** marca produzione neutra con l'icona `rocket` (1 regola, `config.html:28`, `server_form.html:60`, `tunnels.html:46`): segue la sua regola «rosso solo per primario e pericolo».
- **D6** 44 px al tocco (`pointer: coarse`) per tutti i controlli: la soglia del canone.

Per Carlo, la raccomandata per prima:
- **D2 — I segni dentro la mappa** (`_map.html`: ● ○ ■ ✕ ⟳ ⏳ ↑, circa 25 posti in testo SVG con la sua disposizione):
  1. **restano testo in questo contratto** (raccomandata): la mappa ha le sue regole (UX-009) e i segni sono parte del disegno; 0 rischi di spostare l'impaginazione della mappa;
  2. passano ai simboli adesso: ~25 posti in più nella fetta 7, e ogni scheda della mappa va riguardata a 1440 e 400.
- **D3 — L'omino che corre** (`base.html:369-385`, il job in corso):
  1. **corre 5 s, poi si ferma in posa e riparte a ogni passo nuovo del job** (raccomandata): WCAG 2.2.2 rispettata, e il moto torna a dire «è cambiato qualcosa». Un aggiornamento di 3 minuti con 6 passi lo fa correre 6 volte, 30 s in tutto invece di 180;
  2. corre per tutto il job come oggi: zero cambi, ma un movimento di 3 minuti accanto ai log, che la regola dei 5 s vieta.
- **D5 — Mobile-first nelle pagine**:
  1. **valori allineati ai quattro breakpoint adesso, direzione girata quando il template si tocca** (raccomandata): 37 query cambiano valore in una fetta meccanica (la 9); `base.html` e i componenti nuovi sono mobile-first da subito;
  2. tutte le 37 girate adesso: ogni regola da desktop va riscritta come aggiunta sopra quella del telefono, in 15 template; più rischio di rompere un layout, nessun beneficio per chi guarda.

---

## 13. Tracciabilità

- Audit `scratchpad/dx-audit/REPORT.md` del 23/11/2026 (§3 fondamentali, §4 componenti, §6 i dieci cambi, §7 da non copiare), su `4543ded`.
- Decisione di Carlo del 23/11/2026: tutti e dieci i cambi; link di riga e nomi nel colore del testo, sottolineati al passaggio; rosso solo per primario e pericolo.
- Contratti che restano validi e che questo non tocca: UX-001 (parità col pannello di riferimento), UX-006 §2-3 (un primario per vista; testa neutra, primario dentro la scheda), UX-009 (la mappa), UX-013 (menu e workspace), UX-015/016 (ambienti, backup).
- Canone: `~/.claude/rules/odoo-ux.md`, «Dove i pixel sono nostri» (60-30-10, WCAG, due famiglie di font, spazio negativo) e «Il necessario».

## Decise (23/11/2026)

Carlo: tutte e dieci le modifiche; link e nomi di riga nel colore del testo, il rosso solo per il primario e il pericolo. D2, D3, D5 con la raccomandata (i segni della mappa restano testo; l'omino si ferma dopo 5 s e riparte a ogni passo; le media query si allineano adesso ai quattro valori e girano a mobile-first quando il template si ritocca).
