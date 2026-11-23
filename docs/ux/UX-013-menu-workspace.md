# UX-013 — Il menu Workspace, e «Entra come…» sulle righe degli ambienti

Stato: contratto di design, prima della build. File proposto: `/home/carlo/Projects/OmniaCLI/docs/ux/UX-013-menu-workspace.md`. Lo implementa chi scrive `omniacli/templates/base.html`, `workspaces.html`, `_workspaces_list.html`, `instances.html`, `server.html` e `_login_as_dialog.html` (Jinja, JS senza framework, classi esistenti: `.panel`, `.badge`, `.state-dot`, `.small`, `.mono`, `details`, `dialog`, `.hide-sm`). Le stringhe vanno in `omniacli/i18n/it/*.json`.
Valgono le regole comuni e i criteri di UX-001…012, in particolare UX-007 §2, UX-008 §2 regole 1-3 (primario, macchina che non risponde, numero come filtro) e UX-009 §6 (stati, segni e parole): questo contratto li applica e non li ripete.
Origine, due richieste di Carlo del 23/11/2026:
- **A**: «manca un menù con la lista dei workplace che mostra al suo interno i server del workplace, le istanze e gli utenti»;
- **B**: sulla schermata della pagina dell'istanza (testa «Apri Odoo ↗ · Entra come… · Aggiorna · Altro ▾», poi la tabella Ambienti con «Apri ↗» e «⚙ Indirizzi…»): «entra come sarebbe da mettere anche sulla lista delle funzionalità».

---

# Parte A — Il menu Workspace

## A0. Cosa c'è oggi (23/11/2026, 2026.1.5497)

| Fatto | Dove sta oggi | Difetto |
|---|---|---|
| Menu dell'admin della macchina | Server · Istanze · Backup · Impostazioni (`base.html`, `menu_places`) | i workspace non hanno una voce |
| Elenco dei workspace | solo Impostazioni › Persone e accessi › Workspace (`/workspaces`, `Depends(admin)`) | **solo questa macchina**. Sul pannello centrale di Carlo (localhost:8090), che non ha workspace suoi, dice «Nessun workspace ancora» mentre lab1 e lab2 ne hanno. Per arrivare ad `acme` di lab1 servono 4 clic: Server › lab1 › Impostazioni di lab1 › Workspace |
| Colonne dell'elenco | Workspace (con «Limiti e disco ›») · Dominio · **Server** · CPU · Memoria · Rete · Disco usato/limite · Disco reale, più il pannello «Questa macchina» | la colonna «Server» contiene il **numero delle istanze** (`instances_count`); niente stato, niente utenti, niente staging, niente macchine collegate |
| Menu dell'utente di workspace (`scoped`) | Istanze · [Backup] · **una voce per ogni workspace dove è admin** (`my_workspaces`) | con 5 workspace sono 5 voci in più: accrescimento |
| Testa della pagina del workspace | briciole «Impostazioni › Workspace acme» | il workspace è un posto del lavoro, non un'impostazione |
| Dati già pronti | `/api/map` dà, per macchina (collegamenti compresi) e per workspace: etichetta, dominio, `can_open`, `page`, disco (dal registro giornaliero), limiti, **numero** dei membri, istanze con tipo, `parent`, `elsewhere` e stato degli ambienti; e `verdict.watch` per nodo | manca l'elenco dei membri con ruolo e verifica in due passaggi (`two_step` c'è solo in `workspace_page`) |

## A1. Persone e lavoro primario

| Persona | Chi è nel codice | Cosa vede |
|---|---|---|
| **P1 Integratore sul pannello centrale** (Carlo, localhost:8090, nessun workspace suo, collegamenti a lab1 e lab2) | `is_admin`, non `scoped`, `listed_links` non vuoto | tutti i workspace di questa macchina e di ogni collegamento, con la macchina di ognuno |
| **P2 Admin di una macchina collegata soltanto** | (a) entra direttamente nel pannello di lab1, dove è admin; (b) dal centrale, un collegamento che gli dà meno di admin su quella macchina (il ruolo del collegamento limita) | (a) come P1, con i workspace di lab1 e dei collegamenti di lab1; (b) le righe di quella macchina si leggono ma non si aprono |
| **P3 Persona di più workspace** (`scoped`, admin in 2 o più), anche dal portale clienti | `my_workspaces` con 2 o più voci | solo i suoi workspace, senza nomi di macchine |
| P4 Persona di un solo workspace, o solo sviluppatore/utente nei suoi | `scoped` | niente di nuovo (D2) |
| P5 Sviluppatore o utente della macchina | non `scoped`, non admin | niente: `/workspaces` resta `Depends(admin)` |

Device: desktop 1440×900. Il telefono (375×812) serve in sola lettura, per «c'è un cliente con un problema?».

**Lavoro primario**: in 5 secondi «quali clienti ho, dove girano, quale ha qualcosa da guardare, e ci entro». Lavoro secondario: guardare dentro un workspace (macchine, istanze, utenti) senza aprirne la pagina.
**Primario**: **Nuovo workspace** per P1 e P2; nessuno per P3.

## A2. Regole che la parte A aggiunge

1. **Una sola lista dei workspace.** La pagina della voce di menu *è* `/workspaces`. La voce di Impostazioni non diventa una seconda lista (D3).
2. **Nella lista si legge e si apre; si cambia nella pagina del workspace.** L'unica azione che crea è Nuovo workspace (e «Entra come…» della parte B, che non cambia niente in OmniaCLI).
3. **Una macchina che non risponde è una riga con il motivo** (UX-008 r2). Una macchina con un OmniaCLI più vecchio è una riga «da aggiornare», non una macchina giù (UX-009 §6).
4. **«N da guardare» di una riga è lo stesso numero della campanella e della mappa** per i nodi di quel workspace: stessa fonte (`inframap.watch_of`, `names_by_workspace`, `leveled`), nessun conteggio nuovo (UX-008 r3).
5. **Il filtro di chi vede cosa sta nel server**, dentro la route (`visible_servers`, `customer_view`, ruolo nel workspace, ruolo del collegamento). Il JS non nasconde niente che il server abbia mandato.
6. **L'espansione non fa richieste per istanze e utenti**: arrivano con la riga. Solo la riga «Consumo adesso» si legge quando si apre.

## A3. Dove vive (D1)

- **Voce di menu «Workspace»** (EN «Workspaces»), **fra Server e Istanze**: Server · **Workspace** · Istanze · Backup · Impostazioni. L'ordine è quello del contenimento: la macchina contiene i workspace, il workspace contiene le istanze.
  - `title`: «I workspace dei clienti di questa macchina e delle macchine collegate: dove girano, le loro istanze e chi ci entra.»
  - `aria-current` su `/workspaces`, `/workspaces/<ws>`, e dal centrale su `/links/<m>/workspaces/<ws>`.
  - **Attraverso un collegamento il menu resta di chi guarda** (README «Links»): la voce entra in `MENU_PLACES` e in `menu_places()` come posto `workspaces`. Una macchina collegata più vecchia ignora il posto che non conosce: le sue pagine viste da qui non hanno la voce finché non si aggiorna. È accettato e scritto in «Non verificato».
- **P3**: con 1 workspace da admin resta la voce di oggi con il nome del workspace (il percorso più corto). Con 2 o più, **una voce «Workspace» al posto delle N voci** (D2).
- **Impostazioni › Persone e accessi › Workspace**: la scheda va via (D3). I «Template» e «Come funziona» restano in fondo alla lista (§A4.8).
- **Briciole della pagina del workspace**: «Workspace › acme», con «Workspace» che porta alla lista; dal centrale «Workspace › lab1 › acme».

## A4. La pagina, dall'alto

1. **Testa**: `h1` «Workspace»; la riga «I clienti delle tue macchine: dove girano, le loro istanze e chi ci entra.»; «Letta alle gg/mm/aaaa hh:mm»; il primario **Nuovo workspace** (§A6).
2. **Conteggi**: «12 workspace su 3 macchine · ⚠ 2 con qualcosa da guardare». Il secondo è un filtro (`aria-pressed`) che mostra solo quelle righe; è l'unico filtro. Con 0 problemi: «✓ tutti a posto», non cliccabile.
3. **Ricerca**, solo oltre 8 righe (`_list.html`): nome, etichetta, dominio, macchina, nomi delle istanze, nomi degli utenti.
4. **Righe speciali in testa alla tabella**, perché nascondono dati:
   - «lab2 non risponde: <motivo>. I suoi workspace qui non ci sono.», col motivo di `links.describe_error`, e per l'admin il link «Controlla il collegamento ›» a Impostazioni › Collegamenti;
   - «lab2 ha un OmniaCLI più vecchio: aggiornalo per vedere qui i suoi workspace.» (`var(--info)`, segno ↑).
5. **Tabella, una riga per workspace** (alta circa 56 px):

| Colonna | Contenuto | Quando |
|---|---|---|
| Workspace | il pulsante d'espansione ▸, il **nome** (link alla pagina del workspace, attraverso il collegamento se è di un'altra macchina), l'etichetta in `.muted`; sotto, in `.small mono`, il dominio | sempre |
| Stato | «✓ a posto» oppure il segno peggiore con «N da guardare» (✕ in `--err` se almeno uno è critico, ⚠ in `--warn` altrimenti). Il `title` elenca i primi 3 testi | sempre |
| Macchina | la macchina dove vive il workspace; se istanze o database stanno altrove, in `.small` «+ lab2», con `title` «2 staging su lab2» | solo con almeno un collegamento (come la colonna Server di `/instances`); **mai per P3** |
| Istanze | «3 produzioni · 2 staging»; «nessuna istanza». Il limite, se c'è, nel `title`: «3 di 5 consentite» | sempre |
| Utenti | «4»; «—» con il motivo nel `title` quando la riga non si apre (P2b). Con D4: in `.small`, «⚠ 1 amministratore senza verifica in due passaggi» | sempre |
| Disco | «4,1 di 20 GB», dal registro giornaliero dei dischi (nessuna domanda all'helper di root), con la data nel `title`. Oltre il 90 %: in `--warn` con la parola «quasi pieno» | sempre |

   - Ordine: prima le righe con problemi critici, poi quelle con avvisi, poi per nome.
   - **Clic sulla riga** (fuori dal nome): apre o chiude l'espansione. **Clic sul nome**: apre la pagina del workspace. Invio sulla riga espande. Più righe possono stare aperte; quelle aperte stanno nell'URL (`open=<macchina>/<ws>`), così Indietro le ritrova.
6. **«Istanze fuori dai workspace: 3 ›»**, in `.small` sotto la tabella, con link a `/instances`. Solo per l'admin e solo se ce ne sono.
7. **«Senza workspace: questa macchina, lab3.»**, in `.small muted`: le macchine che rispondono e non hanno workspace. Solo per P1 e P2, solo se ce ne sono.
8. **In fondo, chiusi**: «Come funziona ▸» e «Template di questa macchina (N) ▸», come in UX-007 §4.5. Ci sono solo se questa macchina ha il disco per i workspace (`storage_roots`): il pannello centrale senza disco non li mostra.

## A5. L'espansione (sotto la riga, a tutta larghezza)

In quest'ordine:

1. **Da guardare** (solo se ce n'è): fino a 5 righe, ognuna con il testo, «dal gg/mm/aaaa hh:mm» e «Apri ›» verso la pagina dell'istanza o del job; poi «e altri N sulla mappa ›».
2. **Tre blocchi affiancati** da 1100 px in su, uno sotto l'altro sotto quella larghezza:
   - **Macchine** (P1 e P2; **assente per P3**). Una riga per macchina, con quello che ci gira:
     - «lab1 — qui il workspace: 3 produzioni, 1 staging»;
     - «lab2 — 2 staging»;
     - «lab3 — il database di acme-odoo (cluster PostgreSQL 17)», e la replica se c'è.
     Il nome della macchina porta alla sua pagina (Server › lab1).
   - **Istanze**: l'albero di `/instances`.
     - Riga di produzione: il segno e la parola di stato, il nome (link alla pagina dell'istanza), «Odoo 19», la pastiglia del test con il suo stato («Test: in funzione»), ⟳ «in corso: <titolo del job>» se è occupata, e le azioni della parte B.
     - Sotto, rientrate con ↳, le staging: «Staging del 21/11», lo stato, «su lab2» (per P3 «su un'altra macchina»). Una staging ferma dice «ferma» in `--muted`: è una scelta, non un allarme.
     - Il test non è una riga (UX-001 §0).
     - Oltre 6 produzioni: «+N altre ›» verso `/instances` già cercato sul workspace. Rete: una produzione ferma, occupata o con un problema resta sempre visibile.
     - In fondo, «Sulla mappa ›» (`/instances?view=map&workspace=<ws>`, con `machine=` per un'altra macchina).
   - **Utenti** (solo dove la riga si apre, `can_open`). Tre colonne:
     - Nome;
     - Ruolo a parole: Amministratore / Sviluppatore / Utente, oppure il nome del ruolo personalizzato, e in `.small` «su 2 istanze» se la persona è limitata;
     - Verifica in due passaggi: «✓ attiva» oppure «— non attiva». È una parola, non solo un colore.
     Prima gli amministratori, poi per nome. Oltre 8: «+N altri nella pagina del workspace ›» (scheda Utenti). Rete: un amministratore senza verifica resta sempre visibile.
3. **Consumo adesso** (solo l'admin; per P3 dalla route del suo workspace): «CPU 0,42 di 2 core · memoria 1,2 di 4 GB · rete ↓ 12 kB/s ↑ 3 kB/s · disco reale: 120 GB liberi». Si legge all'apertura e ogni 5 s finché l'espansione è aperta e la scheda del browser è visibile. Per un'altra macchina passa dal suo collegamento. Se fallisce: «— non letto: <motivo>».
4. **«Apri il workspace acme ›»**: stesso indirizzo del nome. Sul telefono il nome è lontano, dopo l'espansione.

## A6. Nuovo workspace

- È il primario della testa e c'è per P1 e P2 (a). **Macchine possibili**:
  - questa, se ha il disco per i workspace (`storage_roots` senza `storage_error`);
  - ogni collegamento che risponde, dove chi guarda ha il ruolo admin, e che ha il disco.
- **0 macchine**: il pulsante è disattivato, con il motivo nel `title` dello `span`: «Nessuna macchina può ospitare un workspace: questa non ha disco per i workspace, e i collegamenti non rispondono o non ti danno il ruolo amministratore.»
- **1 macchina**: va dritto al modulo di oggi su quella macchina (UX-007 §4.4 E), attraverso il collegamento se serve.
- **2 o più**: il dialogo «Su quale macchina?».
  - È una scelta a radio: nome della macchina, «N workspace · X GB liberi» quando si sa. Le macchine che non possono sono disattivate, con il motivo accanto.
  - Parte scelta la prima possibile, con questa macchina per prima.
  - Pulsanti: **Continua** (primario del dialogo) e Annulla. Esc chiude e non parte nessuna richiesta.
- Dopo il salvataggio del modulo si apre la pagina del workspace nuovo, dove il primario è Nuova istanza (Peak-End).

## A7. Visibilità

| Elemento | P1 centrale | P2a admin diretto su lab1 | P2b dal centrale, collegamento sotto admin | P3 scoped, admin in ≥2 | P4 | P5 | Meccanismo |
|---|---|---|---|---|---|---|---|
| Voce «Workspace» | sì | sì | sì (è P1 sul centrale) | sì, al posto delle N voci | — (voce col nome, come oggi) | — | `menu_places`, `my_workspaces` |
| Righe di questa macchina | sì | sì | — | i suoi | — | — | `whole` / `mine` come in `/api/map` |
| Righe dei collegamenti | sì | i collegamenti di lab1 | sì, in sola lettura | — | — | — | `listed_links` |
| Colonna e blocco Macchina | sì (con collegamenti) | sì (con collegamenti) | sì | — | — | — | `customer_view` nella route |
| Nome che apre la pagina | sì | sì | — (testo, `title` col motivo) | sì | — | — | `can_open` |
| Utenti (numero ed elenco) | sì | sì | «—» | sì | — | — | `can_open` |
| Consumo adesso | sì | sì | — | sì, il suo | — | — | `Depends(admin)` / route del workspace |
| Nuovo workspace | sì | sì | macchina disattivata nel dialogo | — | — | — | §A6 |
| Template, Come funziona | se questa macchina ha disco | sì | — | — | — | — | `storage_roots` |
| Istanze fuori dai workspace | sì | sì | — | — | — | — | `loose` di `/api/map` |

Il `title` del nome per P2b: «Il collegamento lab2 ti dà il ruolo sviluppatore: la pagina del workspace è degli amministratori.»

## A8. HIDE / DEFER

| Cosa | Dove va | Rete di sicurezza |
|---|---|---|
| Pannello «Questa macchina» (carico della macchina) | Server (`/machines`) | — |
| Grafici di CPU e rete in ogni riga, sempre accesi | riga «Consumo adesso» dell'espansione, letta solo da aperta | il disco, che è il budget, resta nella riga |
| Disco reale | espansione | «quasi pieno» nella riga |
| «Limiti e disco ›» in ogni riga | testa della pagina del workspace, dove c'è già | — |
| Colonna Dominio | `.small mono` sotto il nome | si trova con la ricerca |
| Indirizzi, versioni, backup delle istanze | pagina dell'istanza, `/instances` | i problemi di backup arrivano in «da guardare» |
| Stato degli inviti, passkey, capacità dei ruoli personalizzati | pagina del workspace › Utenti | — |
| Produzioni oltre la sesta, utenti oltre l'ottavo | «+N ›» | i problematici restano visibili |
| Nomi delle macchine per P3 | — | «su un'altra macchina» |
| Template e Come funziona | in fondo, chiusi | solo con disco qui |

## A9. Vuoto, attesa, dati densi

- **Centrale con collegamenti, nessun workspace**: «Nessun workspace sulle macchine collegate. Un workspace è la parte chiusa di una macchina per un cliente: Nuovo workspace ne crea uno sulla macchina che scegli.»
- **Centrale senza collegamenti e senza disco**: «Questa macchina non ha workspace né il disco per crearne. I workspace stanno sulle macchine dei clienti: collegane una da Server › + Aggiungi un server.» Il testo è un link. Il primario è disattivato, con il motivo.
- **Workspace senza istanze**: nella riga «nessuna istanza»; nell'espansione «Nessuna istanza ancora: si crea dalla pagina del workspace (Nuova istanza).», con il link.
- **Workspace senza utenti**: «Nessun utente: entrano solo gli amministratori della macchina.»
- **Ricerca vuota**: «Nessun workspace corrisponde a «…»».
- **Attesa**:
  - prima dei dati si vede lo scheletro delle righe;
  - ogni macchina si chiede per conto suo, in parallelo, come `/instances`;
  - le righe compaiono in **< 2 s** con le macchine che rispondono;
  - un collegamento muto diventa la sua riga entro 5 s (`LIST_TIMEOUT` in tutto);
  - la lista si rilegge ogni 60 s con la scheda visibile, mai con la scheda nascosta.
- **Dati densi**: oltre 50 righe c'è la paginazione di `/instances`. I nomi lunghi si troncano con «…», e il nome intero sta nel `title`.

## A10. Telefono (fino a 700 px)

- Ogni riga diventa una scheda di due righe:
  - riga 1: il nome e lo stato;
  - riga 2, in `.small`: «lab1 · 3 produzioni · 2 staging · 4 utenti · 4,1 di 20 GB».
- L'espansione mette i blocchi uno sotto l'altro. Le azioni delle righe d'istanza seguono la parte B, §B5.
- Niente scorrimento orizzontale. Bersagli tattili alti almeno 44 px.
- Nel menu ☰ la voce sta nello stesso ordine del desktop.

## A11. Il necessario

- **Stato del sistema**: la colonna Stato, «Letta alle…», le righe delle macchine mute o vecchie, lo scheletro, ⟳ sulle istanze occupate.
- **Annulla**: la lista non cambia niente. Il dialogo della macchina si chiude con Annulla o con Esc; il modulo del workspace ha il suo Annulla.
- **Prevenzione dell'errore**: Nuovo workspace disattivato con il motivo; le macchine che non possono sono disattivate nel dialogo con il motivo; P3 non arriva ad altri workspace (filtro nel server, 404).
- **Messaggio d'errore**: i motivi delle macchine sono in italiano (`describe_error`), con il rimedio per l'admin. Una lettura fallita lascia le righe attenuate con «Non aggiornata dalle hh:mm: <motivo>».
- **Aiuto**: il `title` della voce di menu, di ogni pulsante, `summary`, link, intestazione di colonna, filtro e radio del dialogo; gli stati vuoti dicono il primo passo.

## A12. Pulsanti e link (`title` scritto dalla route)

| Elemento | Tipo | Etichetta | `title` |
|---|---|---|---|
| voce di menu | nav | Workspace | §A3 |
| nuovo | **primario** | Nuovo workspace | «Apre il modulo di un nuovo workspace, una parte chiusa di una macchina per un cliente, con disco, limiti, dominio e utenti suoi. Non si crea niente finché non salvi lì.»; con più macchine, in più: «Prima chiede su quale macchina.» (`/api/workspaces/save`) |
| espandi | pulsante ▸ (`aria-expanded`) | — | «Mostra dentro il workspace le macchine, le istanze e gli utenti. Non cambia niente.» |
| nome | link | acme | «Apre il workspace acme: istanze, nomi sul web, utenti e servizi esterni» |
| filtro | link (`aria-pressed`) | ⚠ 2 con qualcosa da guardare | «Mostra solo i workspace con qualcosa da guardare: le stesse cose della campanella e della mappa» |
| macchina | link | lab1 | «Apre la pagina della macchina lab1» |
| sulla mappa | link | Sulla mappa › | «Apre la mappa di questo workspace. Non cambia niente.» |
| +N | link | +N altre › / +N altri › | «Apre l'elenco Istanze cercato su questo workspace» / «Apre la scheda Utenti del workspace» |
| continua | primario del dialogo | Continua | «Apre il modulo del nuovo workspace sulla macchina scelta» |
| annulla | secondario | Annulla | «Chiude senza creare niente» |
| controlla | link | Controlla il collegamento › | «Apre Impostazioni › Collegamenti, dove si prova e si corregge il collegamento» |

## A13. Contratto dei dati (i nomi definitivi li decide chi implementa)

- Come `/api/instances`: una richiesta per macchina, `?link=<nome>`, in parallelo, con `LIST_TIMEOUT` di 5 s **in tutto**. La risposta è `{name, link, workspaces}`, oppure `{name, link, error, older}`.
- La riga riusa il nodo workspace di `/api/map` (la stessa funzione, non una copia). Si aggiungono:
  - i problemi del workspace: le voci di `verdict.watch` dei suoi nodi, con il livello;
  - le macchine dove stanno le sue staging e i suoi database (`elsewhere`, la posizione del database);
  - **`members`, solo con `can_open`**: `[{name, role, custom, limited: n|null, two_step}]`;
  - `creatable: {ok, why}` per Nuovo workspace;
  - per ogni ambiente, il campo `login_as` della parte B.
- Nessuna domanda all'helper di root né a PostgreSQL mentre la richiesta aspetta. Il disco viene dal registro giornaliero.
- La route sta in `WORKSPACE_ROUTES` per P3, filtrata sui workspace dove è admin, con `customer_view`.
- Registro degli accessi: nomi degli utenti e stato della verifica sono dati di sicurezza. Proposta: la route **non** va fra le `NOT_SENSITIVE`. Lo decide chi implementa, con l'architetto.

## A14. Criteri di accettazione (Playwright)

**Dove si prova**:
- la build si prova sulla coppia di sviluppo **lab3/lab4**, con il pannello locale avviato dal worktree e collegato a entrambe;
- l'aspetto finale si prova sul pannello centrale di Carlo con **lab1/lab2**, solo dopo il rilascio del pacchetto.

**Persone**: P1, P2a (admin diretto su lab3), P2b (il token d'ingresso del collegamento su lab4 con ruolo sviluppatore), P3 (una persona admin in 2 workspace di lab3), P5. Niente gruppo developer nelle schermate del cliente. Viewport 1440×900 e 375×812, IT, tema chiaro e scuro.

1. **Menu**:
   - P1: i testi di `.topnav .nav-link:visible` sono, in ordine, [Server, Workspace, Istanze, Backup, Impostazioni];
   - P3 con 2 workspace: 1 voce «Workspace» e 0 voci con il nome di un workspace;
   - P4 con 1 workspace: 0 voci «Workspace» e 1 voce col nome;
   - P5: 0 voci «Workspace»;
   - su una pagina `/links/lab3/…` del centrale, le stesse 5 voci, con `href` senza `/links/`.
2. **Righe**:
   - `tr[data-ws]` = la somma dei workspace delle macchine che rispondono, contati su ognuna con `/root/labapi`;
   - la cella Macchina di ogni riga è la macchina dove il workspace vive;
   - senza collegamenti, l'intestazione «Macchina» c'è 0 volte.
3. **Macchina muta**: con il pannello di lab4 fermo, entro 7 s c'è 1 riga `[data-machine-error]` con «non risponde» e il motivo, e le righe di lab3 non cambiano di numero. Riacceso lab4, le sue righe tornano entro 65 s.
4. **Primario**:
   - `.primary:visible` fuori da `dialog[open]` è 1 per P1 e P2a, 0 per P3;
   - con 2 macchine possibili, il clic apre `dialog[open]` con tanti `input[type=radio]` quante sono le macchine, e 1 `.primary` dentro;
   - Annulla: 0 richieste POST;
   - con 1 sola macchina possibile non si apre nessun dialogo, e l'URL è quello del modulo.
5. **Espansione**:
   - il clic sulla riga mette `aria-expanded=true`;
   - le righe di produzione del blocco Istanze sono quante dice «N produzioni», e le staging quante dice «M staging» (oppure «+N» più le visibili, che fanno lo stesso totale);
   - ogni riga di stato contiene una parola di UX-009 §6;
   - le righe del blocco Utenti sono quante dice la colonna Utenti (fino a 8), e ognuna contiene «attiva» o «non attiva»;
   - aprire l'espansione fa 0 richieste, salvo quella del consumo.
6. **Problemi**:
   - «N da guardare» di ogni riga è uguale al numero delle voci di `/api/map` → `verdict.watch` sui nodi di quel workspace;
   - il filtro lascia esattamente le righe con N > 0.
7. **Riservatezza di P3**:
   - il testo del DOM e le risposte JSON contengono 0 occorrenze di «lab3», «lab4», dei nomi dei collegamenti, di «/var/», «/srv/» e dei nomi degli altri workspace;
   - colonna e blocco Macchina: 0;
   - un altro workspace chiesto nell'URL risponde 404.
8. **P2b**: le righe di lab4 ci sono, con 0 `a` sul nome, Utenti «—», e lab4 disattivata nel dialogo.
9. **Titoli e dialoghi**:
   - ogni `button`, `summary`, `a` della tabella, voce di menu e radio ha un `title` non vuoto e diverso dal testo;
   - 0 chiamate ad `alert`, `confirm` e `prompt`;
   - ogni data rispetta `\d{2}/\d{2}/\d{4} \d{2}:\d{2}`.
10. **Attesa**: con le macchine che rispondono, le righe compaiono in < 2 s, e prima si vede lo scheletro. Con la scheda nascosta: 0 richieste in 30 s.
11. **Telefono**:
    - a 375 px `scrollWidth` ≤ 375;
    - ogni pulsante d'espansione è alto almeno 44 px;
    - le colonne Macchina, Istanze, Utenti e Disco non sono celle visibili, ma la seconda riga della scheda.
12. **Un posto solo**:
    - `/settings` ha 0 schede con `href` `/workspaces` (D3);
    - il primo elemento delle briciole di `/workspaces/<ws>` è «Workspace» con `href` `/workspaces`.
13. **Non cambia niente**: caricando, filtrando, cercando e aprendo tutte le espansioni partono 0 richieste POST, PUT o DELETE.

---

# Parte B — «Entra come…» sulle righe degli ambienti

## B0. Cosa c'è oggi

| Dove | Cosa c'è |
|---|---|
| Testa della pagina dell'istanza (`server.html`) | «Apri Odoo ↗ · Entra come… · Aggiorna · Altro ▾». «Entra come…» agisce sull'ambiente in vista; per un ruolo personalizzato senza la capacità `login_as` è disattivato con il motivo (`refused`) |
| Tabella Ambienti (scheda Panoramica) | per ogni riga (produzione, test, staging) la cella `row-open hide-sm` con «Apri ↗» e «⚙ Indirizzi…». Niente «Entra come…» |
| Elenco Istanze (`instances.html`) | UX-008 §4.1: «nessun pulsante nelle righe». Il nome apre la pagina dell'istanza |
| Regola del server (`login_as_target`) | Odoo 16 o più recente (`has_login`); una **persona** (chi entra qui, o un collegamento per conto di una sua persona; mai un token MCP né un collegamento da solo); **produzione → admin, test → sviluppatore** (`from_link_role`); la capacità `login_as` del ruolo personalizzato (`refusal`). Ogni accesso va nel registro, con l'evento `login-as` |
| Attraverso un collegamento (5471, README «Log in as») | se l'istanza non ha un nome che si apre da fuori, l'OmniaCLI davanti prende la sessione attraverso il collegamento e la dà al browser su un tunnel (`http://127.0.0.1:<porta>/.omniacli/login?t=…`). Serve un tunnel che il browser raggiunge; altrimenti il dialogo offre la riga in nginx |

## B1. Persona e lavoro

- **Persone**: il supporto e l'integratore (admin della macchina, admin di un workspace) e lo sviluppatore sul test.
- **Lavoro**: «entrare in Odoo come l'utente del cliente che ha segnalato il problema», dalla riga dove l'ambiente si vede, senza aprire prima la sua pagina.
- **Device**: desktop.
- Il pulsante è un acceleratore di un'azione che c'è già: stesso dialogo, stessa route, stessa regola, stesso registro.

## B2. Dove sta sulla riga

| Schermata | Righe con «Entra come…» | Posizione |
|---|---|---|
| Pagina dell'istanza › tabella Ambienti | produzione, test, ogni staging (anche quelle su un'altra macchina, attraverso il loro collegamento) | nella cella delle azioni: **Apri ↗ · Entra come… · ⚙ Indirizzi…**, in quest'ordine di frequenza |
| Elenco Istanze | ogni riga di produzione e di staging (il test non è una riga, UX-001 §0) | una cella d'azioni a destra, `hide-sm`: **Apri ↗ · Entra come…** (D5) |
| Lista Workspace › espansione › Istanze | le righe di produzione e di staging; la pastiglia del test no | a destra della riga, come nell'elenco Istanze |

- **Stesso peso di «Apri ↗»**: `button small`, secondario. Mai `.primary`: il conteggio dei primari di ogni schermata non cambia.
- La testa della pagina dell'istanza tiene il suo «Entra come…», come tiene «Apri Odoo ↗» accanto all'«Apri ↗» della riga (Jakob: la coppia testa/riga c'è già).
- Un solo `_login_as_dialog.html` per pagina, che deve servire anche le righe disegnate dal JS (elenco Istanze, espansione). Il titolo del dialogo nomina l'ambiente («Entra come… in acme-odoo · staging del 21/11»).
- UX-008 §4.1, regola «nessun pulsante nelle righe»: per l'elenco Istanze cade, per le due azioni che aprono Odoo. Le azioni che cambiano qualcosa (Riavvia, Backup, Aggiorna) restano fuori dalle righe.

## B3. Chi lo vede (lo decide il server, riga per riga)

| Caso | Sulla riga | Meccanismo |
|---|---|---|
| Odoo 15 o più vecchio, o un plugin senza login | niente | `has_login` |
| Token MCP o API, collegamento senza persona | niente | `acts_for_a_person` |
| Produzione o staging, chi guarda è sotto admin | niente | `from_link_role`: una staging è la produzione del suo server, quindi oggi chiede admin (vedi «Non verificato») |
| Test, chi guarda è sotto sviluppatore | niente | idem |
| Ruolo personalizzato senza `login_as` | pulsante **disattivato**, con il motivo nel `title` dello `span` (lo stesso testo di `refused("login_as")` della testa) | `refusal(me, "login_as", …)` |
| Persona limitata ad alcune istanze | solo sulle sue (le altre non le vede) | `grant` |
| Ambiente fermo | disattivato: «<nome> è ferma: Entra come apre Odoo, che adesso non risponde. Avviala dalla sua pagina.» | stato della riga |
| Ambiente occupato da un job | attivo, come la testa di oggi | — |
| Nessun nome in HTTPS e nessuna via col tunnel (si sa senza chiedere alla rete) | disattivato: «Entra come vuole un nome dell'istanza in HTTPS (Indirizzi…) o un tunnel da questo OmniaCLI: qui non c'è né l'uno né l'altro.» | la stessa decisione di `login_as_target`, presa prima |
| Nginx che non lascia passare `/.omniacli/login` | attivo: lo dice il dialogo, con «Lascia passare a nginx» per l'admin | si sa solo provando la rete: resta nel dialogo |
| Macchina collegata più vecchia, che non manda il campo | niente | campo assente |

## B4. Attraverso un collegamento

- Il pulsante chiama le route della macchina dell'ambiente sotto `/links/<m>/…`: elenco degli utenti e sessione. È il dialogo di oggi, col prefisso.
- Il ruolo è il più basso fra quello di chi guarda e quello del collegamento, come per ogni azione attraverso un collegamento.
- **Istanza senza nome pubblico** (dietro NAT, `localhost:<porta>` là): il pulsante resta attivo, e il `title` cambia: «Apre Odoo in una scheda nuova come uno dei suoi utenti, senza la sua password, tramite un tunnel da questo OmniaCLI a lab4: l'istanza non ha un nome che si apre da fuori.»
- La scheda nuova si apre su `http://127.0.0.1:<porta>/odoo`. Se il browser non raggiunge i tunnel, è il dialogo a dirlo e a offrire la riga in nginx: niente di nuovo sulla riga.
- Il registro degli accessi della macchina dell'istanza scrive l'evento `login-as` con «link <nome> (<persona>)», come oggi.

## B5. Telefono

- Sotto i 700 px la cella delle azioni resta nascosta (`hide-sm`), com'è oggi per «Apri ↗» e «⚙ Indirizzi…»: 0 pulsanti nelle righe.
- Il percorso è un tocco in più: il nome della riga apre la pagina dell'istanza, che in testa ha «Entra come…».
- Due pulsanti accanto a nome e stato non stanno in 375 px senza scorrimento orizzontale. Un menu «⋯» per riga sarebbe un componente nuovo, per un'azione che sul telefono è rara.

## B6. `title` e testi

| Pulsante | Etichetta | `title` (da `login_as_target` e `_login_as_dialog.html`) |
|---|---|---|
| riga, nome pubblico | Entra come… | «Apre Odoo di <ambiente> in una scheda nuova come uno dei suoi utenti, senza la sua password: lo scegli nell'elenco. Non cambia niente nel database; l'accesso resta nel registro degli accessi.» |
| riga, tramite tunnel | Entra come… | §B4 |
| disattivato | Entra come… | il motivo di §B3, sullo `span` |

## B7. Il necessario

- **Stato del sistema**: il dialogo mostra il caricamento degli utenti; la scheda nuova si apre su Odoo; il motivo del pulsante disattivato.
- **Annulla**: «Chiudi» del dialogo, oppure Esc, senza richieste POST. La sessione fatta finisce se l'utente cambia la password (README).
- **Prevenzione dell'errore**: disattivato quando si sa già che non può funzionare (§B3). Il server rifiuta comunque, come rete.
- **Messaggio d'errore**: i rifiuti di `login_as_target` (403 e 409) passano da `tr`, con il rimedio: «Indirizzi…», «Lascia passare a nginx».
- **Aiuto**: il `title` di §B6.

## B8. Criteri di accettazione (Playwright, lab3/lab4 con il pannello locale)

Dati: un'istanza Odoo 19 su lab3 con test e 1 staging, tutto in funzione; una staging su lab4 di una produzione di lab3; un'istanza su lab4 senza nome pubblico; 1 staging ferma; un ruolo personalizzato senza `login_as`.

1. **Ambienti, admin della macchina**:
   - `[data-login-as]` nella tabella è 3: produzione, test e staging;
   - in ogni cella è il fratello subito dopo «Apri ↗» e subito prima di «⚙ Indirizzi…»;
   - `.primary:visible` della pagina è lo stesso numero di prima.
2. **Ruoli**:
   - sviluppatore del workspace: 1, sul test;
   - utente del workspace: 0;
   - ruolo personalizzato senza `login_as` (preset admin): 3 pulsanti `disabled`, ognuno in uno `span[title]` con il testo di `refused`;
   - token MCP su `/api/servers`: 0 campi `login_as`.
3. **Elenco Istanze, P1**:
   - i pulsanti sono tanti quante le righe di produzione e di staging di Odoo 16 o più recente in funzione, più quelle ferme come `disabled`;
   - le righe dei collegamenti hanno `data-login-as` con un URL che comincia con `/links/<m>/`.
4. **Espansione della lista Workspace**: 1 pulsante per ogni riga di produzione e di staging; 0 sulla pastiglia del test.
5. **Dialogo**:
   - il clic apre `dialog[open]` con almeno 1 utente e il titolo con il nome dell'ambiente;
   - «Chiudi» non fa partire nessuna richiesta POST;
   - scegliendo un utente di un'istanza con nome HTTPS (o sslip.io), la pagina nuova arriva a `/odoo`;
   - il registro degli accessi di lab3 ha 1 voce `login-as` in più, con la persona e l'istanza.
6. **Tunnel**: sull'istanza di lab4 senza nome pubblico, dal pannello locale, il `title` contiene «tunnel», e la pagina nuova arriva a `http://127.0.0.1:<porta>/odoo`.
7. **Fermo**: la staging ferma ha il pulsante `disabled` e lo `span[title]` contiene «ferma».
8. **Telefono, 375 px**:
   - `[data-login-as]:visible` nelle tabelle e nelle espansioni è 0;
   - in testa alla pagina dell'istanza è 1;
   - `scrollWidth` ≤ 375.
9. **Titoli**: ogni «Entra come…» ha un `title` (o uno `span[title]`) non vuoto e diverso dal testo, nella lingua della pagina.

---

## Smell che fanno FAIL (per `odoo-ux-tester`)

- Due liste dei workspace (Impostazioni e menu) con dati diversi.
- Una macchina muta che sparisce invece di diventare una riga con il motivo.
- P3 che vede il nome di una macchina, di un collegamento o di un altro workspace.
- «N da guardare» diverso dalla campanella o dalla mappa per lo stesso workspace.
- Uno stato affidato al solo colore; una staging ferma segnata come allarme.
- Un `.primary` in più in una riga, o «Entra come…» come primario.
- «Entra come…» attivo dove il server lo rifiuterebbe per un motivo già noto (ruolo, fermo, nessun nome né tunnel).
- Un pulsante che cambia qualcosa (Riavvia, Backup, Aggiorna) aggiunto alle righe.
- Scorrimento orizzontale a 375 px.
- Gli screenshot presi solo come admin della macchina, o con il gruppo developer.

## Decisioni di Carlo (la raccomandata per prima)

1. **D1 — Nome e posto della voce.**
   - **Raccomandata: «Workspace» fra Server e Istanze.** Il menu dell'admin passa da 4 a 5 voci. Da `acme` su lab1 si passa da 4 clic a 1 (la voce) più 1 (il nome).
   - Alternativa: dopo Istanze, con lo stesso numero di voci, ma l'ordine non segue più il contenimento.
   - Alternativa: nessuna voce, e una scheda «Workspace» nella pagina di ogni macchina. I workspace di tutte le macchine resterebbero senza una lista unica.
2. **D2 — La persona di più workspace.**
   - **Raccomandata: con 2 o più workspace da admin, una voce «Workspace» al posto delle N voci.** Con 5 workspace il suo menu perde 4 voci. Con 1 workspace resta com'è oggi.
   - Alternativa: le N voci restano, e in più c'è «Workspace»: diventano 6 voci.
   - Alternativa: la voce solo agli admin della macchina; le N voci restano.
3. **D3 — Impostazioni › Workspace.**
   - **Raccomandata: la scheda va via e `/workspaces` è la lista del menu.** CPU, memoria e rete passano nell'espansione («Consumo adesso»), il disco resta nella riga.
   - Alternativa: la scheda resta e porta alla stessa pagina: due ingressi per una lista sola.
   - Alternativa: due pagine (quella dei consumi in Impostazioni, quella del contenuto nel menu): due liste della stessa cosa.
4. **D4 — Un amministratore senza verifica in due passaggi.**
   - **Raccomandata: un avviso nella cella Utenti** («⚠ 1 amministratore senza verifica in due passaggi»), **fuori** da «N da guardare». Così la campanella non riceve un'altra fonte di rumore: il 22/11 le cose da guardare erano 47, quasi tutte rumore.
   - Alternativa: dentro «N da guardare», e quindi anche nella campanella e sulla mappa.
   - Alternativa: solo la colonna «Verifica» nell'espansione.
5. **D5 — Elenco Istanze: solo «Entra come…» o la coppia con «Apri ↗».**
   - **Raccomandata: la coppia «Apri ↗ · Entra come…»**, come nella tabella Ambienti (Jakob). Sono 2 pulsanti per riga su desktop, 0 sul telefono.
   - Alternativa: solo «Entra come…», come chiesto: 1 pulsante per riga, e Odoo si apre dalla pagina dell'istanza.

## Non verificato

- Se `/api/map` per macchina, con 60 istanze, sta nei 2 s (aperto anche in UX-009). Se non ci sta, la riga del workspace va in cache con la sua data.
- Se una macchina collegata più vecchia ignora davvero il posto `workspaces` nell'intestazione del menu senza errori: va provato con lab2 a una versione precedente.
- **Una staging chiede admin per «Entra come…»**: `from_link_role` guarda il ruolo dell'ambiente, e una staging è la produzione del suo server. Il README dice «un test è di uno sviluppatore, una produzione di un admin», e non nomina le staging. Il contratto non lo cambia. Se Carlo vuole la staging per lo sviluppatore, è una modifica della route, non della vista.
- Se il dialogo di oggi si aggancia anche alle righe disegnate dopo il caricamento: oggi lega `[data-login-as]` una volta sola, al caricamento della pagina.

## Tracciabilità

Due richieste di Carlo del 23/11/2026, in chat. Non ci sono un `TS-NNN` né un `T<n>`/`HT<n>`.
- UX-001 §0 (il test non è una riga);
- UX-007 §4.4-4.5 (pagina e lista dei workspace, D1 di allora);
- UX-008 §2 regole 1-3 e §4.1;
- UX-009 §6 (stati) e §13 (`/api/map`).

Fonti nel codice:
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/base.html`: il menu, righe 559-605;
- `/home/carlo/Projects/OmniaCLI/omniacli/app.py`:
  - `menu_places` (173) e `MENU_PLACES` (290);
  - `my_workspaces` (217);
  - `/workspaces` (4857) e `workspace_page` (5083, `two_step`);
  - il nodo workspace di `/api/map` (12905-12921);
  - `/instances` e `/api/instances` (12536-12558), `listed_links` e `link_list` (12487-12510);
  - `login_as_target` (3676), `login_as_buttons` (2457-2473), `from_link_role` (3058);
  - le schede di Impostazioni (10864);
- `/home/carlo/Projects/OmniaCLI/omniacli/capabilities.py`: `login_as` (37, 51, 180);
- `/home/carlo/Projects/OmniaCLI/omniacli/inframap.py`: `LEVELS`, `watch_of`, `names_by_workspace`;
- `/home/carlo/Projects/OmniaCLI/README.md`: «Log in as» (3764) e «Operating another instance» (842).
