# OmniaCLI contro il DX Playbook di Toyota Europe (23/11/2026)

Misurato su `carlo_dev_2026` a `4543ded` (77 template) e sugli schermi di lab3 (Playwright, admin, italiano, 1440/400, chiaro/scuro). Playbook: 222 pagine della sitemap + il CSS dello styleguide (`middleware.*.css`). Schermate in `shots/`, misure in `measure.json` e `dom-metrics.json`.

## 1. Filosofia
1. Use the platform: HTML semantico, `<dialog>` con `showModal()`, `<table>` con `<caption>` e `scope`.
2. Token fino in fondo: primitivi (non si usano) e semantici (`foreground/neutral/subtle`).
3. CUBE CSS e composizione.
4. Accessibilità come contratto (WCAG 2.1 AA): 4,5:1 testo, 3:1 bordi/icone/fuoco; colore mai unico segnale; fuoco visibile e diverso dall'hover; bersaglio 44, mai sotto 24.
5. Movimento: 5 durate, 2 curve, `prefers-reduced-motion` sempre, niente loop oltre 5 s senza pausa.
6. Gerarchia: un solo primario per vista, etichette con un verbo, titoli che scendono un livello alla volta.
È il 少少軽短美 / Nielsen di `odoo-ux.md`, con in più le **scale chiuse**.

## 2. Marchio (non si applica) / regola generale (si segue)
Non si applica: loghi, font Toyota Type/Nobel, palette teal/copper/rosso, raggi 0/8/10, bottoni 50/38/32, icone di settore, `62.5%` e divieto di `rem`, accent/AI/social button, energy label, model card, carosello.
Si segue: primitivi separati dai semantici; livelli emphasis/default/subtle/muted (muted solo disabilitato); tabella delle coppie testo-superficie; scale chiuse di spazi, raggi, contorni, ombre, durate, breakpoint; tipo a gradini con interlinea in token; un set di icone; tre livelli di pulsante; WCAG per componente.

## 3. Fondamentali
- **Loghi**: non applicabile; 26×26 (`base.html:115`) va bene.
- **Colore: in parte.** Semantici sì (`base.html:51-103`), primitivi no; scuro scritto due volte (`:74-87`, `:88-103`); 27 esadecimali fuori token; `login.html`/`invite.html` ricopiano la palette. Contrasti (chiaro/scuro): `--muted` su `--bg` 4,47/7,34; su `--panel-2` 4,63/6,21; `--side-muted` 3,75/4,22; `--border-strong` (bordo campi) 1,47/1,73 (serve 3); anello di fuoco accento 60% 2,63/3,14; fuoco campi ombra 18% 1,33 con `outline:none` (`:276-277`); «Test» `--light-green` 3,30. Ok: `--link` 6,47; bianco su primario 4,83. Il rosso è insieme primario, link, pericolo, avviso: colonne di nomi rosse sembrano errori. Cambio: `--fg-subtle` #5F6673; `--border-input` #868E9C (scuro #667085); `--side-muted` #8794A6; un `--focus-ring: 2px solid #B91C1C` offset 2 anche sui campi; link di riga in `--text` sottolineati all'hover (scelta di Carlo); scuro scritto una volta, palette login/invito comune.
- **Tipografia: no.** 18 dimensioni in 321 dichiarazioni (13px ×185, 12 ×52, 14 ×26, 15 ×14, 11 ×13…), 9 interlinee, pesi 650/800; 89 dei 494 `style=` fissano un corpo. 44% del testo a 1440 sotto 14 px. Cambio: `--fs-12/14/16/18/20/24`, interlinee 18/20/24/28/32, 13→14, 12,5/11→12, 15/17/19→16/18/20, pesi 400/600/700; testo da leggere (spiegazioni nei dialoghi, help, `.page-head p`) a 16.
- **Icone: no.** Caratteri ed emoji (✓ ×28, ↗ ×20, ⚠ ×17, ✕ ×14, ▾ ×12, › ×110, 🔔 ☰ ☾/☀ ×). Cambio: sprite `<symbol>` in base.html, 16/20 px, `aria-hidden` sulle decorative.
- **Movimento: in parte.** 6 durate di transizione, 12 di animazione; `m-pulse`/`m-shine` in `machines.html` (~29) infinite senza riduzione. Cambio: `--dur-100…400`, `--ease-out cubic-bezier(.23,1,.32,1)`, `--ease-in-out (.77,0,.175,1)`, una regola di riduzione su `:root`.
- **Responsività: no.** 12 query desktop-first (700 ×13, 640 ×6, 600 ×6, 760 ×4, 900, 560, 1000, 1279, min 701/1100). Nessuna pagina scorre in orizzontale a 400. Difetti: sotto 560 «Esci» e lingua spariscono e non sono nel ☰ (`base.html:143-145`); in Backup «Azioni ▾» copre «Staging»; la barra delle schede scorre senza segno. Cambio: 576/768/992/1200; Esci e lingua nel ☰ (`base.html:584-620`); sfumatura sulle `.tabs`; badge di `backups.html:32`.
- **Spaziatura: in parte.** Nessun token; 85% già in scala (0,2,4,6,8,10,12,16,20,24,32,40,48…); fuori: 14 ×52, 18 ×29, 3 ×27, 22 ×18, 5 ×13, 9 ×12. Cambio: `--sp-2…40`; 14→12/16, 18→16/20, 22→24, 3→2/4, 5→4/6, 9→8/10.
- **Forma: in parte.** `--r` 8, `--r-lg` 12; 22 raggi a mano; contorni 3 px; 3 ombre a mano. Cambio: `--r-sm 4`, `--r 8`, `--r-lg 12`, `--r-pill`; 3→2/4 px; `--shadow-md` menu/tooltip, `--shadow-lg` dialoghi/toast.

## 4. Componenti
- **Pulsanti**: primario unico rispettato 12/12. Altezze 33 / small 26 / barra 28; a 1440 18% sotto 24 px, 97% sotto 44. 9 varianti di colore (start, stop, restart, activate, test, edit, clone, current, primary danger, `base.html:429-443`). Cambio: primario, secondario, terziario (ghost), pericolo; `min-height` 36, small 32, `.icon-btn` 32×32.
- **Schede**: 0 gestori delle frecce; attiva e inattiva peso 600 (`base.html:302-304`). Cambio: script comune frecce/Home/End con tabindex roving; attiva 700; padding 8/12.
- **Tabelle**: 65 tabelle, 267 `th`, 0 caption, 0 scope; intestazioni MAIUSCOLE 12 px in `--muted`. Cambio: `scope="col"`, `aria-label`, intestazioni con l'iniziale maiuscola, 600.
- **Card**: 4 padding diversi (`.panel` 20, `.server-head` 12/16, `.tile` 12/14, `.machine-top` 10/14). Cambio: un `--card-pad` 16.
- **Dialoghi**: 53, `showModal` ovunque, `method="dialog"` 28, `aria-labelledby` 41; nessuna X; su telefono restano centrati; «Aggiorna moduli» impila i pulsanti anche su desktop. Cambio: X comune (macro + gestore), `aria-labelledby` nei 12 mancanti, foglio dal basso sotto 576, padding 24.
- **Campi**: 285 label (166 con `for`), 101 placeholder, 3 `aria-describedby`, 0 `aria-invalid`; bordo 1,47:1; checkbox 13 px. Cambio: describedby/invalid, `--border-input`, `--focus-ring`, checkbox 18 px con `accent-color`.
- **Badge**: pillola 12 px senza icona. Cambio: icona su ok/warn/error, padding 2/8.
- **Menu**: «Altro ▾» 13 voci in 5 gruppi (playbook 4-12); CSS `.menu` a hover morto (`base.html:184-193`); ▾ non gira. Cambio: togliere il morto, freccia SVG con `aria-expanded`, valutare Console e Sostituisci il database in pagina.
- **Avvisi**: `.banner` senza icona; paragrafi arancioni sciolti. Cambio: icona, paragrafi → `.banner.warn` (~25 posti).
- **Navigazione**: ok, salvo Esci/lingua su telefono.
- **Caricamento**: testi «Lettura…» (Database > 2 s). Cambio: `.skeleton-line/.skeleton-block` per database, moduli, backup.
- **Tooltip**: 757 `title=` con frasi intere; invisibili su telefono e al fuoco. Da decidere con Carlo: il contenuto resta; l'essenziale in vista o in un toggletip «?», gli irreversibili nel testo della conferma.

## 5. Misure (1440 chiaro): interattivi / <24 / <44 / primari / corpi / testo <14
Server 23/3/23/1/10/46 su 68 · Macchina Panoramica 32/12/32/1/11/28 su 84 · Database 19/1/19/0/8/9 su 29 · SSH 21/2/21/1/9/23 su 47 · Istanza Panoramica 69/10/69/1/9/36 su 107 · Backup 58/10/58/1/8/25 su 93 · Moduli 49/5/48/1/8/36 su 88 · Impostazioni 68/8/68/0/9/69 su 154 · Istanze 44/18/44/1/7/33 su 92 · Workspace 24/10/24/1/9/63 su 144 · Backup globale 95/17/95/0/7/112 su 205 · Impostazioni globali 29/1/13/0/7/28 su 54. A 400: 0/12 scorrono in orizzontale; 90/446 interattivi sotto 24.

## 6. I 10 cambi, dal più visibile
1. Campi e fuoco visibili (bordo 3,3:1, anello 6,47:1). ~6 regole.
2. Il rosso solo per primario e pericolo; link di riga in `--text`. ~12 posti. **Decide Carlo.**
3. Scala dei caratteri: 6 dimensioni, 5 interlinee, testo da leggere 16. ~330 posti.
4. Pulsanti per peso: 4 varianti, 36/32, icon-btn 32. ~60 posti.
5. Telefono: Esci e lingua nel ☰, badge Backup. 3 posti.
6. X nei 53 dialoghi + foglio dal basso sotto 576.
7. Icone SVG al posto di emoji/caratteri. ~200 posti.
8. Schede con frecce, attiva in grassetto.
9. Testo secondario e lingua ≥ 4,5:1 (2 token).
10. Scale di spazi, raggi, durate, breakpoint (~280 sostituzioni meccaniche).

## 7. Da non copiare
Logo, font, palette, superfici EV/bz, accent button; raggi 0; bottoni 50/38/32; titoli giganti; 62.5% e divieto di rem; carosello, model card, energy label, social/AI button; 16 px per tutto (tabelle e liste restano 14).

## 8. Non letto
`/resources` (403); pagine React/Setup; CSS dei componenti non mappati; componenti senza equivalente; `/demos`, `/playground`; «Color › Tokens» valore per valore; regole di posa del logo.
