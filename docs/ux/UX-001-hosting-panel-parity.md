# UX-001 … UX-005 — OmniaCLI alla pari con un pannello di hosting

Stato: contratto di design, prima della build. Implementa: chi scrive i template (`omniacli/templates/`),
stringhe in `omniacli/i18n/it/*.json`. Modello dati invariato (ROLES = production/test, stagings.json).
Origine: tre obiezioni di Carlo sulla stessa forma («siamo all'età della pietra», «nei pulsanti manca
tutta l'usabilità», «e se avessi una produzione e più stage?»): non si mette una quarta toppa, si cambia
la forma degli schermi.
Riferimento: il pannello di hosting, pagina server (screenshot 2026-11-17 21-43-36).

## 0. Il concetto sullo schermo: una produzione e le sue copie

Chi usa lo schermo vede **un'istanza Odoo = una produzione + le sue copie**.
Le copie sono di due tipi, nello stesso elenco e con la stessa riga:

| Copia | Da dove viene nel codice | Cosa sa fare in più |
|---|---|---|
| **Test** (una, fissa) | il ruolo `test` dello stesso server | *Porta in produzione* (`POST /servers/{s}/merge`), *Ricopia dalla produzione* (azione `copy-production` del plugin Odoo) |
| **Staging** (0…N, datate) | un server fratello `<nome>-stg-<aaaammgg>`, `stagings.json` | niente in più: si usa e poi si elimina |

«Test è solo una staging in più» si adotta con due correzioni:

1. Test porta il badge **«Test»**, non «Staging». Ha due capacità che una staging non ha: il merge del suo
   codice verso la produzione e il ricaricamento dalla produzione. Con lo stesso nome, l'aiuto al passaggio
   del mouse di quei pulsanti sarebbe falso su metà delle righe.
2. **Il test di una staging non si mostra mai.** Il codice lo crea e lo ferma subito (`staging_create`).
   Rete di sicurezza: se quel test risulta acceso o ha un job in corso, la pagina della staging mostra una
   riga d'avviso, «Il test di questa staging è acceso e consuma risorse.», con il pulsante **Ferma**.

Nell'elenco delle istanze della macchina (UX-002) le righe figlie sono **solo le staging**, come su
il pannello di riferimento. Test esiste per ogni istanza: ripeterlo come riga figlia raddoppierebbe l'elenco senza dire
niente di nuovo (Hick). Test si raggiunge dalla pagina dell'istanza.

## Regole comuni (valgono per tutti gli schermi)

- **Un solo pulsante `.primary` visibile per stato**. Nessuno mentre un job lavora sull'istanza.
- **Ogni pulsante e ogni voce di menu ha un `title`** che dice cosa fa, cosa cambia e quando non si può
  usare. I testi stanno nelle tabelle qui sotto e sono scritti leggendo la route.
  Un pulsante disattivato ha il `title` su uno `<span>` intorno, perché un `<button disabled>` non mostra
  il tooltip (lo schema c'è già per *Copia da collegamento*).
- **Date**: `gg/mm/aaaa hh:mm`, mai l'ISO con la `T`. Durate: `1 min 30 s`, senza andare a capo.
- **Nessun testo inglese nell'interfaccia italiana.** Eccezione: i nomi dei livelli di log nel tab Log e
  il contenuto del log stesso. Fughe trovate oggi:
  - «gives no way to find its processes» (fonte del campionatore);
  - lo stato grezzo del job «success» nella tabella job della pagina server;
  - «Log docker compose logs odoo»;
  - i titoli dei job generati in inglese;
  - i rifiuti di `refuse(...)` mostrati grezzi dal JS della staging (`data.error`).
- **Stato a parole + pallino** in tutti gli schermi, con gli stessi quattro valori:
  - **In esecuzione** (verde);
  - **Ferma** (rosso);
  - **Job in corso** (ambra, link al job);
  - **Stato sconosciuto** (grigio), con `title` «OmniaCLI non riesce a leggere lo stato del servizio».
- **Testo d'errore**: cosa non è andato + cosa fare, in italiano. Esempio: «inaria-odoo ha un job in
  corso: riprova quando finisce.». Mai l'inglese di `refuse()` così com'è.
- **La fascia «Istanza collegata inaria»** resta, perché dice su quale macchina si agisce (Nielsen 1), ma
  diventa neutra, non rossa: il rosso è per il pericolo. Il nome della macchina diventa la prima voce delle
  briciole di pane.

---

## UX-001 — Elenco macchine (`machines.html`)

- **Persona e device**: Carlo o un admin, desktop.
- **Lavoro primario**: vedere quali macchine sono vive e aprirne una.
- **Sopra la piega, in ordine**: titolo «Macchine» e contatori (N server, N online) → card. Ogni card:
  nome; stato (pallino + parola); host; CPU e Disco con le barre; «N istanze · N core · OmniaCLI
  versione». **Tutta la card è il link «Apri».**

| Pulsante | Decisione | Tipo | Etichetta | Aiuto al passaggio del mouse |
|---|---|---|---|---|
| Aggiungi un server | tieni | **primario** (l'unico) | + Aggiungi un server | «Prepara una nuova macchina: OmniaCLI vi si collega via SSH con la sua chiave e vi installa ciò che serve. Disattivato finché questa installazione non ha una chiave di provisioning.» |
| Apri | togli come pulsante, diventa la card cliccabile | — | (card) | `title` sulla card: «Apri inaria: le sue istanze, il suo carico, i suoi job» |
| Workspace | sposta nel menu ⋯ | secondario | Workspace | «Apri i workspace di inaria: dischi, limiti, domini e membri» |
| Prova | tieni nel menu ⋯ | secondario | Prova il collegamento | «Controlla che inaria risponda dal suo collegamento; non cambia niente» |
| Rimuovi | tieni nel menu ⋯ | pericolo | Rimuovi dall'elenco… | «Toglie inaria da questo elenco: cancella solo il collegamento. La macchina e le sue istanze restano come sono.» (`/api/links/delete`) |
| Riprova (stato failed) | tieni | primario della card fallita (unica eccezione) | Riprova | «Riapre la preparazione dal passo che è fallito» |
| Log (in preparazione o fallita) | tieni | secondario | Vedi il job | «Apre il job di preparazione di questa macchina» |

- **Il necessario**: stato con pallino + parola aggiornato a ogni giro; *Rimuovi* chiede conferma e il
  collegamento si rifà da *Aggiungi*; *Aggiungi* disattivato se manca la chiave, con il motivo nel `title`;
  errore nella card in italiano; aiuto nei `title`.
- **Vuoto**: il pannello «Aggiungi il tuo primo server» (primario), e allora niente primario in testata.

## UX-002 — Pagina della macchina = Dashboard (`index.html`, `_servers_part.html`)

- **Persona e device**: Carlo o un admin, desktop; secondari un utente del ruolo `user` (avvia, ferma,
  riavvia) e un membro di workspace (vede le sue istanze).
- **Lavoro primario**: vedere a colpo d'occhio quali istanze Odoo girano e aprirne una.
- **Sopra la piega, in ordine**
  1. **Intestazione**: nome macchina, stato (Connessa / Non raggiungibile), host, «OmniaCLI <versione>»,
     «N core». A destra, solo per admin, il **primario «+ Nuova istanza»**.
  2. **Tab** (al massimo 4): **Panoramica** · Job · Workspace (admin) · Dischi (admin).
  3. **Tre card numeriche**: Istanze Odoo (N); In esecuzione (n di N, rossa se n < N); Disco usato (%).
  4. **Barre di risorse** come il pannello di riferimento: CPU, Disco, Rete (Memoria se il campionatore la misura).
     Sotto, «aggiornato alle hh:mm:ss». I grafici della macchina dietro «Dettagli ▸», chiuso.
  5. **Tabella «Istanze Odoo»**, ordinata per nome:

     | Colonna | Contenuto |
     |---|---|
     | Istanza | nome in grassetto, link alla sua pagina (UX-003); sotto, il dominio con ↗ che apre Odoo in una nuova scheda |
     | Stato | lo stato della **produzione**, pallino + parola |
     | Odoo | la versione (dal template, es. 19.0), se nota |
     | CPU 15 min | una sola sparkline, quella della produzione |

     Le staging stanno **subito sotto la loro produzione**: «↳ nome [Staging] copiata il 17/11/2026».
     **Nessun pulsante nelle righe.**

| Pulsante | Decisione | Tipo | Etichetta | Aiuto al passaggio del mouse |
|---|---|---|---|---|
| Nuova istanza | tieni | **primario** | + Nuova istanza | «Crea una nuova istanza Odoo dalla sola versione: un workspace suo, una produzione e un test; con un dominio, la produzione risponde lì e il test su test.<dominio>. Disattivato se il disco non ha almeno lo spazio minimo.» |
| Dischi | diventa tab | — | Dischi | — |
| Produzione / Test nella riga | togli | — | — | il nome porta alla pagina; il dominio ↗ apre Odoo |
| Avvia / Ferma / Riavvia nella riga | togli (decisione di Carlo) | — | — | restano sulla pagina dell'istanza |
| Modifica gruppi | sposta in Impostazioni | — | — | — |
| Nuovo workspace | sposta nel tab Workspace | — | — | — |
| Tutti i job | diventa tab Job | — | — | — |

| Da nascondere | Dove va | Rete di sicurezza |
|---|---|---|
| Colonna Workspace | nascosta | visibile se le istanze stanno in più di un workspace |
| Riga Test sotto ogni istanza | non c'è | Test è sulla pagina dell'istanza |
| Sezione «Ultimi job» | tab Job | una riga «Ultimo job: <titolo> — riuscito/fallito, alle hh:mm» con il link |
| Sezione «Workspace» | tab Workspace | — |
| Grafici della macchina | «Dettagli ▸» | — |
| Ricerca | — | compare con più di 8 righe |

- **Il necessario**: card, pallini e riga dell'ultimo job, aggiornati quando un job parte o finisce;
  macchina non raggiungibile → card con «—» e «inaria non risponde dal suo collegamento: <motivo>»;
  `title` sul nome, sul dominio ↗ e sul badge Staging.
- **Vuoto**: «Nessuna istanza Odoo su questa macchina.» con «+ Nuova istanza» dentro il pannello.
  **Molte righe**: ricerca sopra 8, paginazione sopra 50.

## UX-003 — Pagina dell'istanza: produzione (`server.html`)

- **Persona e device**: Carlo (admin o developer), desktop. Secondari: ruolo `user`, membro di workspace.
- **Lavoro primario**: portare il codice nuovo in produzione e vedere che gira; poi gestire le copie.
- **Sopra la piega, in ordine** (1440×900)
  1. Briciole `inaria › inaria-odoo`; titolo; riga di fatti: stato · dominio ↗ · Odoo 19.0 · workspace.
     **Una colonna sola**: niente produzione e test affiancati.
  2. **Riga dei pulsanti** a destra del titolo: al massimo 5 visibili, l'ultimo è «Altro ▾».
  3. Con un job in corso: **fascia** «Job in corso: <titolo> — <passo>» con il link, pulsanti disattivati.
  4. **Tab** (al massimo 5): **Panoramica** · Log · Backup · Monitoraggio · Impostazioni. Monitoraggio solo
     se la fonte misura.
  5. **Panoramica**:
     - **«Copie di questa produzione»**: Test per prima, poi le staging dalla più nuova. Colonne: Copia
       (nome o dominio ↗, link) · Tipo (badge Test/Staging) · Copiata il · Stato. In testata il
       secondario **«+ Nuova staging»**.
     - **«Ultimi job»**: 3 righe. Azione · Esito (pallino + parola; errore in rosso sotto) · Quando · Durata.
       Sotto, «Tutti i job di inaria-odoo».

| Elemento | user | developer | admin |
|---|---|---|---|
| Aggiorna, Backup, Altro › Codice | — | sì | sì |
| Riavvia, Avvia, Ferma | sì | sì | sì |
| + Nuova staging | — | — | sì (solo istanza fatta in un workspace) |
| Altro › Sostituisci il database | — | Copia da collegamento solo su un test | sì |
| Tab Impostazioni, Backup | — | sì | sì |

| Stato | Primario |
|---|---|
| In esecuzione, developer o admin | **Aggiorna** |
| In esecuzione, ruolo `user` | **Riavvia** |
| Ferma | **Avvia** |
| Stato sconosciuto | **Riavvia** |
| Job in corso | nessuno; tutto disattivato, con il motivo nel `title` |

| Pulsante | Decisione | Tipo | Etichetta | Aiuto al passaggio del mouse (dal codice) |
|---|---|---|---|---|
| Git pull + riavvio | tieni, rinomina | **primario** in esecuzione | Aggiorna | «Scarica il codice nuovo dei repository (git pull), poi aggiorna i moduli Odoo cambiati* e riavvia la produzione. Se i repository sono più d'uno, prima chiede quali. Non fa un backup prima: per un cambio grande premi Backup. Non disponibile mentre un job lavora su questa istanza.» (*solo con `after_pull` attivo; per un'istanza da pacchetto: «Installa la versione nuova del pacchetto <nome> e riavvia.») |
| Riavvia | tieni | secondario (primario per `user`) | Riavvia | «Riavvia Odoo della produzione: chi è collegato perde la connessione per qualche secondo. Non cambia né il codice né il database.» |
| Avvia | tieni | **primario** quando è ferma | Avvia | «Avvia Odoo della produzione.» |
| Backup | tieni | secondario | Backup | «Salva database e filestore della produzione in un file zip su questa macchina; nel dialogo le scelte, per esempio senza filestore. Il file compare nel tab Backup.» |
| Accedi come… | tieni | secondario | Entra come… | «Apre Odoo in una nuova scheda come uno dei suoi utenti, senza la sua password.» |
| Ferma | in Altro | secondario | Ferma | «Ferma Odoo della produzione. Resta fermo, anche per l'aggiornamento automatico, finché non premi Avvia.» |
| Aggiorna moduli | in Altro › Codice | secondario | Aggiorna moduli… | «Aggiorna i moduli cambiati, oppure quelli che scegli (-u), e, se lo spunti, riavvia. Non scarica codice nuovo.» |
| Git pull | in Altro › Codice | secondario | Solo git pull | «Scarica il codice nuovo senza riavviare: Odoo continua con il codice di prima finché non lo riavvii.» |
| Backup + download | in Altro › Backup | secondario | Backup e scarica | «Fa un backup e scarica lo zip appena è pronto.» |
| pip requirements / click-odoo | in Altro › Strumenti (solo con venv) | secondario | — | i tooltip esistenti, tradotti |
| Shell / Claude / psql | in Altro › Console | secondario | — | i `title` esistenti, tradotti |
| Ripristina | in Altro › Sostituisci il database | pericolo | Ripristina un backup… | «Sostituisce il database della produzione con uno zip dei suoi backup o caricato dal tuo computer. Per confermare va scritto il nome del database. Solo admin.» |
| Copia da collegamento | in Altro › Sostituisci il database | pericolo | Copia da collegamento… | «Fa un backup di <collegamento>, lo scarica e lo ripristina qui al posto del database attuale. Per la produzione va scritto il nome del database.» |
| Merge di test in produzione | nella pagina Test (UX-004) | — | — | — |
| Copia produzione in test | nella pagina Test (UX-004) | — | — | — |
| Selettore Test / Produzione / Entrambe | togli | — | — | `?view=both|production` → produzione; `?view=test` → pagina Test |
| + Nuova staging | nella sezione Copie | secondario | + Nuova staging | «Crea <nome>-stg-<aaaammgg>: fa adesso un backup della produzione e lo carica, neutralizzato (niente azioni pianificate né posta in uscita), in una nuova istanza accanto a questa. La produzione resta accesa. Non disponibile mentre un job lavora sulla produzione, o se il workspace ha già tutte le istanze che gli spettano.» |

| Da nascondere | Dove va | Rete di sicurezza |
|---|---|---|
| Tile CPU/Memoria/Processi + 2 grafici | tab Monitoraggio | il tab non c'è se la fonte non misura; compare al primo punto |
| Log | tab Log | se l'ultimo job è fallito, la fascia d'errore porta «Vedi il log» |
| Elenco backup | tab Backup | — |
| «Impostazioni e repository» | tab Impostazioni | un repository fuori dal branch atteso dà una riga d'avviso in Panoramica |
| Descrizione sotto il titolo | via | — |
| Ultimi job: colonne Server, Avviato da, Creato ISO, Finito | via | restano nella pagina del job |

- **Il necessario**: stato in testata, fascia del job, ricarica a fine job; *Porta in produzione* fa un
  backup prima e il job riuscito lo dice; conferma con il nome dell'istanza su Aggiorna, Ferma e Riavvia
  in produzione, nome del database su Ripristina e Copia da collegamento; rifiuti in italiano sotto la riga
  dei pulsanti, con il cosa fare.
- **Vuoto**: Copie senza staging → la riga Test e «Nessuna staging: una staging è una copia datata della
  produzione, per provare un aggiornamento senza toccarla.»; istanza fuori da un workspace → niente
  *Nuova staging*; «Nessun backup ancora.»; «Nessun job ancora.». Oltre 5 staging: «Mostra tutte (N)».

## UX-004 — Pagina di una copia: Test e staging

Stessa struttura di UX-003 (stesse posizioni, stesso primario per stato).

**Test** (`/servers/<s>?view=test`): briciole `inaria › inaria-odoo › Test`, badge **Test**, stato,
dominio test ↗. Lavoro primario: provare il codice e portarlo in produzione.

| Pulsante | Decisione | Tipo | Etichetta | Aiuto al passaggio del mouse |
|---|---|---|---|---|
| Aggiorna | come UX-003 | **primario** in esecuzione | Aggiorna | come UX-003, «il test» al posto di «la produzione» |
| Merge (admin) | qui, visibile | secondario, pericolo | Porta in produzione… | «Fa un backup della produzione, unisce nei suoi repository i commit che girano ora sul test (il commit provato, non la punta del branch), li pubblica (push), aggiorna i moduli e riavvia la produzione. Si ferma se il test ha modifiche non committate o commit non pubblicati. Non disponibile mentre un job lavora sulla produzione o sul test.» |
| Copia produzione in test | in Altro › Sostituisci il database | pericolo | Ricopia dalla produzione… | «Carica nel database del test un backup della produzione (nuovo o uno già fatto), neutralizzato, poi aggiorna i moduli e avvia il test. Il database attuale del test si perde.» |

**Staging** (`/servers/<nome>-stg-<data>`): briciole `inaria › inaria-odoo › inaria-odoo-stg-20261117`,
badge **Staging**, sotto «Copia di **inaria-odoo** del 17/11/2026 22:48, dal backup <zip>» (link alla
produzione). **Si vede solo la sua produzione**: niente test, *Nuova staging*, *Porta in produzione*,
*Ricopia dalla produzione*, selettore di vista.

| Pulsante | Decisione | Tipo | Etichetta | Aiuto al passaggio del mouse |
|---|---|---|---|---|
| Elimina (`/api/workspaces/{ws}/instances/delete`, admin) | aggiungi in Altro | pericolo | Elimina staging… | «Elimina questa staging: contenitori, database, file e cartelle, poi la toglie dall'elenco. La produzione da cui è copiata non cambia. Non si annulla. Non disponibile mentre un job lavora sulla staging.» Conferma scrivendo il nome; a job finito si torna alla produzione |

## UX-005 — Pagina del job, dopo un'azione (`job.html`)

- **Lavoro primario**: sapere se è andata e tornare al proprio lavoro.
- **Sopra la piega**: briciole `inaria › inaria-odoo › <azione>`, titolo in italiano; fascia di stato
  grande (In coda / In corso… con passo e barra / Fatto / Non riuscito: <errore in italiano>); a job
  riuscito il **primario «Torna a <istanza>»** («Scarica <file>» per Backup e scarica, «Apri <nuova
  istanza>» per Nuova staging e Nuova istanza); riga dei fatti compatta; log in fondo.
- *Ferma il job*: secondario, pericolo, con conferma; `title` «Termina il programma del job (SIGTERM, poi
  SIGKILL dopo 10 s): quello che ha già fatto resta fatto, un passo interrotto resta a metà.»
- Via: la descrizione; chip dei livelli, filtro e A−/A+ dietro «Filtri ▸»; campi vuoti; chiavi grezze
  `inaria-odoo/production` sostituite dai nomi delle copie.

---

## Criteri di accettazione (Playwright)

Come **admin**, ruolo **`user`** e **membro di workspace**; viewport 1440×900 e 375×812; lingua IT.

1. In ogni pagina e stato, `.primary` visibili = 1 (0 con un job in corso). Le card di UX-001: 0 (tranne
   una card fallita).
2. UX-002: zero `button` e `form` dentro `table.server-rows tr`; una staging ha `↳` e il badge «Staging» ed
   è subito dopo la sua produzione; niente righe Test.
3. UX-003/004: al massimo 5 pulsanti sopra la piega, al massimo 5 tab, una colonna d'istanza, niente
   selettore di vista; log non visibile in Panoramica; senza misure niente tab Monitoraggio.
4. Ogni pulsante e voce di «Altro» visibile ha un `title` non vuoto e diverso dall'etichetta.
5. Staging: assenti «Nuova staging», «Porta in produzione», «Ricopia dalla produzione» e il pannello del
   test; «Elimina staging…» chiede il nome. Sulla produzione la sezione Copie elenca Test + N staging, la
   più nuova prima.
6. Nessun match di `/\b(the|of|gives|no way|success|failed|running|stopped|Production|Test & activate)\b/`
   fuori dal Log; nessuna data `\d{4}-\d{2}-\d{2}T`.
7. Ruolo `user` su una produzione in esecuzione: primario «Riavvia»; Aggiorna, Backup e Nuova staging
   assenti.
8. Job riuscito: un primario «Torna a …», «Apri …» o «Scarica …»; job fallito: errore in italiano.

## Ordine di implementazione (per impatto)

1. UX-003: una colonna, un primario per stato, «Altro», tab; via il selettore di vista.
2. §0 + UX-003/004: sezione Copie, pagina della staging ridotta alla sua produzione, *Elimina staging*.
3. Regole comuni: `title` su ogni pulsante, stringhe IT, date `gg/mm/aaaa`.
4. UX-002: intestazione, card, barre, tabella senza pulsanti, staging come figlie.
5. UX-005 e UX-001: «Torna a …» a fine job, card cliccabile con un solo primario.

Fuori da questo contratto: la barra laterale da 13 voci, da raccogliere in «Impostazioni».

## Decisioni di Carlo (18/11/2026)

1. Test nelle copie con il suo badge «Test».
2. Nessun pulsante Avvia/Ferma/Riavvia nelle righe dell'elenco istanze.
3. Nomi a schermo come il pannello di riferimento: «Server» le macchine, «Istanze» i server Odoo (solo etichette e Guida).
4. Eliminare una produzione resta nel Workspace; una staging si elimina dalla sua pagina.
5. Staging di una staging: non offerta (scelta raccomandata, non chiesta).
