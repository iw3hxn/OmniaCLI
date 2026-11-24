# AD-017: Un pannello centrale e un agente sottile su ogni macchina

Stato: **accettata da Carlo il 24/11/2026, con il portale nel centrale (AD-018 da scrivere)**. Carlo ha detto sì all'idea il 24/11. Le domande aperte sono in §10.

Il codice è stato letto alla revisione `d74373a` (`carlo_dev_2026`), più la modifica non committata di
`omniacli/templates/server.html`, cioè il pulsante «Scarica» della riga dei backup. Le misure di questo documento
vengono dal codice e da `git log` di quella revisione. Dove un numero è stimato, lo dice.

Le regole di sempre valgono e qui non si ripetono:
- i dati del cliente stanno sul disco del suo workspace (AD-002);
- ogni azione è un job nel registro degli accessi della macchina che agisce (NIS 2);
- un segreto passa al helper sullo standard input;
- un dato del fornitore non compare dalla parte del cliente (`customerview.py`);
- la produzione si aggiorna solo a mano;
- REST `/api/v1` e MCP vanno sempre insieme, per ogni funzione.

Origine: Carlo, 24/11/2026, in chat: «ha senso o è meglio concentrare tutto?». Il caso è il pulsante «Scarica» nella
riga dei backup. Sul pannello centrale non si vede per una macchina di un cliente a 5508 finché quella macchina non si
aggiorna. Non ci sono `TS-NNN` né `T<n>`/`HT<n>`.

## 0. Il problema e il costo, in numeri

**Come funziona oggi.** Il pannello centrale (`localhost:8090`, un OmniaCLI come gli altri) mostra le pagine di un'altra
macchina con `link_proxy` (`app.py`, `/links/{name}/{path}`). Chiede la pagina all'OmniaCLI di quella macchina e
inoltra al browser l'HTML che quella ha scritto (`StreamingResponse`). La pagina è quindi scritta dal codice installato
**sulla macchina**: una correzione arriva solo quando la macchina si aggiorna.

**Il caso di oggi.** Il campo `download` della riga di un backup è nell'API della macchina dal 23/11 (`09caf8e`,
`app.py` riga 3272). Il pulsante sta solo nel template, nel JavaScript della scheda Backup di `server.html` (+8 righe).
Il dato c'è già sulla macchina, manca solo la pagina.

| Cosa | Quanto | Fonte |
|---|---|---|
| Collegamenti del pannello centrale | 7, tutti via SSH (5 macchine di clienti, lab1, lab2) | `~/omniacli-local/links.yaml`, letti solo nomi e tipo |
| Commit su `omniacli/` dal 17/11 (8 giorni) | 233 | `git log` |
| ... che toccano solo template, testi o static | 42 (18%): con il centrale arrivano a tutte le macchine senza aggiornarle | `git log --name-only` |
| ... che toccano helper di root, `ops.py`, `treesync`, `machinedb`, repliche, packaging | 102 (44%): chiedono comunque l'aggiornamento della macchina | idem |
| ... il resto (`app.py` e altri moduli Python, senza helper) | 89 (38%): in parte contesto di pagina (centrale), in parte API (agente). Non separabili senza leggerli uno per uno | idem |
| Aggiornare oggi una correzione di pagina su tutte le macchine | 7 aggiornamenti a mano, ciascuno con ≈ 20 s di pannello assente e il nome scritto se la macchina serve produzioni | README, Update OmniaCLI |
| Stato del pannello centrale da salvare | 17 MB (`links.yaml`, utenti, sessioni, passkey, `ssh/` con 15 file, job, log), esclusi venv e backup | `du` |
| Lavoro stimato di tutto il piano di §9 | ≈ 30 giornate in 9 fette, ognuna rilasciabile da sola | stima, §9 |

La settimana misurata è quella della costruzione di AD-008, piena di helper nuovi. La quota dei commit «solo pagina» in
una settimana normale **non è misurata**.

---

## AD-017: La forma

**Contesto.** Ogni macchina ha un OmniaCLI completo. Il centrale non scrive le pagine delle altre macchine, le inoltra.
Il pacchetto è uno solo (`omniacli`, `pyproject.toml` 2026.1.x), e il cliente che riceve un workspace (AD-008…AD-015)
installa lo stesso pacchetto.

**Opzioni considerate** (le scartate, con il motivo, sono in §11):
- A. **Pagine, contesto di pagina e testi nel centrale. Dati, API, helper e autorizzazione sulla macchina.** Il centrale
  scrive con i **suoi** template le pagine delle altre macchine, con i dati chiesti all'API della macchina attraverso il
  collegamento di oggi. Se il contratto di versione non torna, la macchina scrive ancora la sua pagina come oggi.
- B. Tenere tutto com'è e solo firmare il self-update.
- C. Una SPA statica servita dal centrale.
- D. Il centrale che spinge i template alle macchine.
- E. Un agente in un pacchetto separato.
- F. Tutto nel centrale, anche il portale dei clienti e l'identità.

**Decisione.** A, in sei parti (17.1-17.6).

**Motivazione.**
- 少: niente pacchetto nuovo né repo nuovo. Lo stesso codice scrive la pagina da un'altra parte, e il collegamento, il
  prefisso `/links/<m>` (214 usi di `prefix` nei template) e le API ci sono già.
- 美: è la forma che il codice già suggerisce. Le liste globali (Istanze, Backup, Mappa) sono già scritte dal centrale
  con i dati chiesti ai collegamenti (`/api/instances?link=`, `/api/backups?link=`, `/api/map`).
- 軽: la macchina non chiama mai il centrale, e un centrale spento non tocca niente di quello che serve Odoo.

**Contratto d'invarianti** (le proprietà che il riuso dei template di oggi presume):

| Invariante | Vale? |
|---|---|
| Template e dati sono della stessa versione (oggi li scrive la stessa macchina) | **No**: il centrale sarà più nuovo o più vecchio della macchina. Per questo serve il contratto di 17.2, con il ritorno alla pagina della macchina |
| Gli indirizzi nelle pagine partono da `prefix` | **Sì**: 214 usi nei template, 28 `prefix_of(` in `app.py` |
| Il contesto di una pagina è fatto di dati semplici | **No per 7 pagine**: `server.html` legge 164 volte attributi di oggetti vivi (`Instance`, plugin: 22 attributi diversi), `workspace.html` 33, `config.html` 27, `job.html` 23, `links.html` 22. Serve un modello di pagina esplicito (17.1) |
| Le decisioni su chi può cosa le prende la route, non il template | **Sì**: i template non controllano l'utente (0 `me.can` fuori da `help`, `options`, `credentials`). Arrivano già `denied`, i pulsanti, i limiti. Restano calcolati sulla macchina |
| I dialoghi dei plugin stanno sulla stessa macchina della pagina | **Non sempre**: due dialoghi del plugin `odoo` sono nel pacchetto (`update_dialog.html`, `copy_dialog.html`), ma un plugin di terzi installato solo sulla macchina il centrale non lo ha. In quel caso la pagina torna a quella della macchina |
| Le collezioni nel contesto sono limitate | **Sì oggi** (job `[:10]`, registro accessi `[:500]`, log `logtail.MAX_LINES`) e deve restarlo: vincolo 17.1.4 |

**Tracciabilità.** Chat di Carlo del 24/11; i vincoli di AD-008 §8 e di AD-014.

### 17.1: Cosa si sposta: l'inventario delle pagine

I template sono 86 file, 25.843 righe: 43 pagine e 43 parziali. Il JavaScript delle pagine è **dentro i template**:
in `static/` c'è solo xterm. Le route `GET` che scrivono una pagina sono 41:
- 37 `@app.get`, contando una volta il ciclo delle shell;
- `/login`, `/login/code`, `/login/setup`;
- `/about`, `/oauth/consent`, `/invite/{token}`, `/api/v1/docs`.

In `app.py` (28.194 righe) le registrazioni di route sono:
- 146 `@app.get` (105 sotto `/api/`);
- 240 `@app.post`, 2 `put`, 1 `delete`, 2 `api_route`, 3 `websocket`;
- 27 `add_route`;
- 21 su `/api/v1`.

MCP ha 41 strumenti.

| Classe | Pagine | Righe (pagina sola) | Cosa serve |
|---|---|---|---|
| **A: guscio e JS che legge `/api`** (contesto ≤ 5 chiavi di soli interruttori, 0-3 cicli) | 12: `disks` 207, `log` 30, `own_log` 23, `instances` 302, `backups` 772, `machines` 956, `discover` 134, `connection` 218, `shell` 32, `help` 255, `about` 16, `settings` 25 | 2.970 | Quasi gratis: il centrale scrive il proprio template con `prefix=/links/<m>`, e il JS chiama `/links/<m>/api/…` come oggi. `instances` e `backups` lo fanno già |
| **B: liste e moduli piccoli scritti dal server** (3-21 chiavi, dati semplici) | 14: `jobs` 24 (+ `_jobs_table` 27), `job` 314, `config` 100, `credentials` 37, `enterprise` 149, `links` 272, `tunnels` 111, `groups` 146, `plugins` 126, `destinations` 181, `access` 129, `account` 261, `schedules` 327, `workspaces` 920 | 3.124 | Un modello di pagina per ognuna (≈ 0,4 g l'una) |
| **C: pesanti, con oggetti vivi** | 7: `server` 3.286 (**6.324** con 11 parziali; 50 chiavi), `index` 832 (**4.453** con 10 parziali, tra cui `_machine_database` 2.115), `workspace` 1.189 (1.602; 48 chiavi), `users` 482 (27 chiavi), `pullers` 622 (18), `server_form` 510 (16), `options` 546 (14) | 7.467 | Lavoro vero: un modello di pagina esplicito per gli attributi letti |
| **D: restano sempre sulla macchina** (identità e accesso di quella macchina) | 9: `login`, `oauth_consent`, `oauth_message`, `invite`, `error`, `link_error`, `placed_error`, `moved_away`, `api_docs` | n/d | Niente |
| Layout | `base.html` 1.377 | n/d | Quello del centrale (menu, marchio, lingua del lettore): sparisce l'artificio di `X-OmniaCLI-Menu` |

I file più pesanti:

| File | Righe |
|---|---|
| `server.html` | 3.286 |
| `_machine_database.html` | 2.115 |
| `_map.html` | 1.774 |
| `base.html` | 1.377 |
| `workspace.html` | 1.189 |
| `machines.html` | 956 |
| `workspaces.html` | 920 |
| `index.html` | 832 |
| `backups.html` | 772 |
| `pullers.html` | 622 |

**Decisione.** Ogni route di pagina delle classi B e C si divide in due:
- il **modello di pagina**, calcolato sulla macchina con il `Principal` del collegamento (ruolo abbassato, `on_behalf`).
  Contiene i dati, `denied`, i pulsanti e i limiti;
- la **scrittura**, fatta da chi mostra la pagina.

La macchina espone i modelli in un'API interna, `GET /api/pages/<pagina>/…`, che risponde solo al token di un
collegamento. Il centrale la chiede e scrive il proprio template.

**Vincoli.**
1. Il modello di pagina è un **elenco esplicito di campi**, mai un dump di un dataclass. `Instance` e le opzioni portano
   password e token: un test cerca nel JSON di ogni modello i nomi dei segreti noti (`admin_passwd`, `db_password`,
   `token`, `secret`) e il risultato deve essere 0.
2. L'autorizzazione resta sulla macchina: `denied`, `capabilities` e `refusal` si calcolano lì, come in AD-014 (due
   controlli). Il centrale non allarga mai quello che il modello permette.
3. `/api/pages/*` non sta in `WORKSPACE_ROUTES` e non è una funzione del prodotto: non ha un gemello in `/api/v1` e in
   MCP. Le azioni restano le route di oggi, che hanno già il loro REST/MCP dove serve.
4. Le collezioni senza limite (job, backup, registro, log) non entrano intere nel modello: restano le API paginate di
   oggi, lette dal JS.

### 17.2: Il confine agente/pannello e il contratto di versione

**Sulla macchina (l'agente):**
- gli helper di root (`workspace-helper`, `compose-helper`, `pitrhelper`, self-update), Docker, nginx, PostgreSQL;
- i backup e le loro destinazioni, le pianificazioni (`schedules.yaml`), monitor e avvisi, webhook della CI;
- le API interne `/api/*`, `/api/v1`, il MCP della macchina e i modelli di pagina;
- l'autorizzazione e il registro degli accessi;
- utenti e portale dei workspace (17.4);
- le API macchina-macchina di consegna e spostamento (`/api/handover/v1`, `/api/placements`).

**Nel centrale:** i template, il layout, i testi italiani, il contesto di presentazione (ordine, raggruppamenti,
etichette), le liste globali, le sessioni dei suoi utenti e i token dei collegamenti.

**Il contratto.** Oggi una macchina «più vecchia» si riconosce solo perché `/api/map` risponde 404 (`MAP_OLDER`,
`app.py` riga 14110), e non esiste un numero di versione dell'API. Si aggiunge:
- `GET /api/info` risponde anche `agent_api` (un intero, 1 alla prima fetta) e `plugins` (`{nome: versione}`). Una
  macchina che non lo dice vale `agent_api = 0`: oggi sono tutte così;
- ogni template del centrale dichiara il minimo che gli serve (`needs_agent_api`). Il centrale conosce il massimo che sa
  leggere (`CENTRAL_AGENT_API`);
- **la regola**: il centrale scrive la pagina se `needs_agent_api ≤ agent_api ≤ CENTRAL_AGENT_API` e ha tutti i plugin
  della macchina, nelle stesse versioni. Altrimenti inoltra la pagina della macchina, come oggi, con una riga sopra:
  «Pagina scritta da <macchina> (2026.1.5508): aggiornala per vedere quella nuova» se la macchina è indietro, «… il
  pannello è più vecchio della macchina» nel caso opposto;
- dentro lo stesso `agent_api` l'API cambia **solo aggiungendo** (campi, route). Togliere o rinominare alza il numero.
  Il centrale legge almeno `agent_api` e `agent_api − 1`. Un campo che manca spegne il pulsante, come già fa la riga del
  backup (`f.download ? a : button disabled`);
- `agent_api` si legge con «Prova il collegamento» e poi ogni 10 minuti, con il valore tenuto in memoria. Si rilegge
  subito dopo un aggiornamento fatto dal pannello.

**Conseguenze.** Una correzione di sola pagina arriva a tutte le macchine con `agent_api ≥ 1` al riavvio del centrale.
Una macchina si aggiorna solo quando cambiano l'`agent_api` che una pagina chiede, un helper o la sicurezza.
`Update OmniaCLI…` di una macchina resta com'è: chiesto da una persona admin di lì, mai dal centrale da solo.

### 17.3: Le modalità del pacchetto

**Opzioni.**
- A. **Un pacchetto, un'impostazione di root**: `panel: full | agent` in `/etc/omniacli/omniacli.yaml`, con default
  `full`.
- B. Due pacchetti: vedi l'alternativa E di §11.
- C. Nessuna modalità: decide tutto il centrale, per collegamento.

**Decisione.** A, con il minimo di differenze. In più, nel centrale, per ogni collegamento `pages: here | machine`
(default `here` da 17.2 in poi): è l'interruttore che annulla tutto senza un rilascio.

**`full`** (il default di oggi, e quello del cliente che riceve un workspace). Tutto come oggi: le sue pagine, i suoi
collegamenti, il suo portale. Il centrale di Carlo è un `full` con i collegamenti.

**`agent`** cambia tre cose, e nient'altro:
1. Le pagine di amministrazione della macchina restano servite e funzionanti: sono la **riserva** di 17.4. Dicono in
   testa «Questa macchina si gestisce da <nome del centrale>».
2. Il token del collegamento del centrale riceve dalla macchina solo JSON, file scaricati e websocket (shell, tunnel),
   più l'HTML delle pagine **solo per il ritorno** di 17.2. Il controllo lo fa **il centrale** (17.5): una macchina
   compromessa non lo può togliere.
3. «Aggiorna OmniaCLI» si propone come necessario solo quando `agent_api` è dietro a quello che il centrale chiede.
   Prima resta un'informazione.

**Come si passa.** Si cambia una riga in `omniacli.yaml` e si riavvia: nessun dato si sposta, perché i dati non hanno
mai lasciato la macchina. All'indietro è lo stesso. La consegna (AD-008…AD-013) chiede `full` alla macchina che
**riceve** (lo dice `requirements` del manifesto, AD-009). La macchina che consegna può essere `agent`: le sue API di
consegna sono API. Per lo spostamento (AD-014, AD-015) casa e host possono essere `agent` tutte e due: si parlano solo
con le API.

### 17.4: Con il centrale fermo, e come si protegge

**Cosa continua, centrale spento.** Sulla macchina c'è già tutto e non dipende dal centrale (l'agente non ha un token
verso il centrale, come l'host in AD-014):
- Odoo e i suoi database;
- le pianificazioni, i backup verso le destinazioni, il monitor e gli avvisi;
- i webhook della CI;
- **il portale dei clienti** (17.4.1);
- REST e MCP di ogni macchina con i token di lì;
- consegna e spostamento già avviati, macchina con macchina.

**Cosa si ferma:** le pagine di Carlo, le liste globali, la mappa, la campanella d'insieme, MCP `call_linked` del
centrale.

**17.4.1: Il portale dei clienti resta sulla macchina.** Utenti dei workspace, inviti, secondo fattore, passkey,
`customerview` e la redazione restano dove sono oggi: sulla casa del workspace (AD-014). Il centrale non diventa una
casa. Il prezzo: una correzione di una pagina **vista dal cliente** arriva al cliente con l'aggiornamento della sua
macchina, come oggi. Le pagine raggiungibili da un utente di workspace sono 28 route non-API su 143 di
`WORKSPACE_ROUTES`. Portare il portale nel centrale è una scelta di Carlo (§10.2).

**17.4.2: La riserva.** Ogni macchina ha il pacchetto intero, quindi le sue pagine ci sono sempre. Ci si arriva con
`ssh -L 8090:127.0.0.1:<porta> <macchina>` e un utente admin **locale** della macchina, con il secondo fattore. Vincolo:
ogni macchina in `agent` ha almeno un admin locale. Il passaggio a `agent` lo rifiuta senza.

**17.4.3: La protezione del centrale.**
- Backup notturno cifrato dello stato del centrale (17 MB) su una destinazione esistente, sigillato come
  `pullcrypto.py`, AES-256-GCM. La passphrase non resta sul centrale.
- Ripristino provato: una macchina nuova, il backup, il centrale che apre una macchina. Tempo da misurare in F7, con
  obiettivo ≤ 15 min.
- Un secondo centrale acceso è una scelta di Carlo (§10.5). Se c'è, ha **token suoi** su ogni macchina: si revoca da
  solo, e il registro dice quale centrale ha agito.

### 17.5: Sicurezza: il centrale tiene le chiavi di tutti

**Com'è oggi** (misurato):
- il centrale tiene già 7 token di collegamento **in chiaro** (`LinkTarget.token`, `links.yaml` 600) e le chiavi SSH dei
  collegamenti (`ssh/`, 15 file);
- sulle macchine c'è solo l'hash (`sha256$`);
- tutti e 7 i collegamenti passano da SSH (`sshlinks.py`), con la chiave dell'host fissata alla prima connessione e un
  utente che può solo inoltrare: è già un'autenticazione reciproca;
- mTLS non c'è (0 occorrenze), e per un collegamento HTTPS c'è il `ca_file`.

**Il rischio che c'è già oggi, e che la decisione riduce.** `link_proxy` serve l'HTML e il JS scritti dalla macchina
**sotto l'origine del centrale**. Passa `content-type` così com'è, e la CSP è solo `frame-ancestors 'none'`. Una macchina
compromessa può quindi eseguire script con la sessione di Carlo sul centrale, e da lì raggiungere tutte e 7. Con le
pagine scritte dal centrale la macchina fornisce solo JSON, inserito con l'autoescape di Jinja.

**Decisione.**
1. Per un collegamento `pages: here` il centrale inoltra solo `application/json`, i download dichiarati e i websocket.
   L'HTML della macchina passa solo nel ritorno di 17.2, che resta il rischio di oggi finché quella macchina non si
   aggiorna.

   **Com'è nel codice (fette 1-2, `centralpages.is_data`).** Sulle risposte a `/links/<m>/api/…` e alle richieste dei
   percorsi delle 12 pagine A, il centrale lascia passare:
   - i tipi `application/json` e `application/*+json`, `application/x-ndjson`;
   - `text/plain` e `text/csv`;
   - le immagini raster (`png`, `jpeg`, `gif`, `webp`, `x-icon`), **mai** `image/svg+xml`;
   - `application/octet-stream`, `zip`, `x-tar`, `gzip`, `sql`;
   - qualunque risposta con `Content-Disposition: attachment`;
   - le risposte senza corpo (204, 304, `Content-Length: 0`, e senza tipo né lunghezza solo se il corpo letto è vuoto).

   Tutte escono con `X-Content-Type-Options: nosniff`. Il resto (HTML, script, SVG, XML) è rifiutato con 502 e una
   riga `failed` nel registro. A una richiesta `/api` del browser il centrale chiede `Accept: application/json`, così
   la macchina risponde con il suo motivo e non con la sua pagina d'errore.

   **Cosa passa ancora come HTML della macchina:** dalla fetta 5 solo le pagine che il centrale non scrive: quella
   di un invito (di chi entra lì) e le pagine proprie di un plugin (`/x/<plugin>/…`); con `pages: machine`, tutto.
   Nelle pagine scritte qui (le 12 A, l'istanza, le 14 B, le altre C) l'HTML della macchina passa solo nel ritorno di 17.2
   (con la riga in testa), e solo finché quella macchina non ha mai detto `agent_api ≥ 1`: il valore più alto detto da
   ogni collegamento è tenuto su disco (`link-agents.json`, nel purge). Dopo, se dice meno o non ha il modello della
   pagina, il centrale mostra una sua pagina che lo dice, finché un amministratore non mette `pages: machine`. Un
   centrale più vecchio della macchina o un plugin diverso tornano ancora alla pagina della macchina sulla sua parola:
   resta il rischio di oggi, per quella macchina.
2. Nessun mTLS. Il trasporto resta SSH: la chiave dell'host fissata vale quanto un certificato client più la CA, e c'è
   già su 7 collegamenti su 7. Un collegamento HTTPS nuovo continua a usare `ca_file`.
3. Le sessioni del centrale valgono quanto tutte le macchine. Il secondo fattore per gli utenti del centrale è §10.4.
4. Il token del centrale resta di ruolo admin (serve per agire). Il ruolo della persona si abbassa con
   `X-OmniaCLI-Role-Cap`, come oggi.
5. Registro NIS 2:
   - la macchina registra ogni lettura di modello e ogni azione con il collegamento, `on_behalf` = la persona e il
     workspace;
   - il centrale registra chi ha chiesto, da quale sessione.

   Le letture di dati del cliente o di sicurezza sono già classificate in `accesslog.py`, e valgono anche per
   `/api/pages/*`.

### 17.6: Macchine dietro NAT

Oggi una macchina che nessuno raggiunge si opera con un **puller**: comandi e report attraverso il relay
(`puller.py`, `consumer.py`), non con le sue pagine. `relay.py` porta i nomi di Odoo con `ssh -R` verso il loopback del
relay, con una chiave limitata a `permitlisten` di una porta.

**Decisione.** Lo stesso tunnel inverso porta anche l'API dell'agente:
- una seconda porta del loopback del relay, con la sua chiave e la sua riga `permitlisten`;
- il centrale ci arriva con un collegamento SSH al relay (`permitopen` di quella porta);
- l'agente apre il tunnel verso l'esterno, e il centrale non apre porte.

Con questa strada la macchina dietro NAT ha le stesse pagine delle altre. I puller restano per chi non vuole nemmeno
questo. Il relay vede solo il traffico SSH cifrato.

---

## 8. Vincoli per il dev-lead

1. Le pagine A e i modelli B/C seguono 17.1. Il modello di pagina è un elenco di campi, e c'è il test che non trova
   segreti nel JSON.
2. Il contratto di 17.2: `agent_api` in `/api/info`, `needs_agent_api` per template, la regola di scrittura o ritorno,
   solo aggiunte dentro un numero. Un test di contratto scrive ogni pagina del centrale con i modelli registrati di
   `agent_api` N e N−1 (fixture).
3. **Parità**: per ogni pagina spostata, un test confronta i pulsanti visibili e spenti della pagina scritta dal
   centrale con quelli della pagina della macchina. Si fa per admin, developer, user e un ruolo personalizzato (come
   `tests/test_portal_access.py`). Il risultato deve essere 0 differenze.
4. La macchina non chiama mai il centrale. I tempi restano quelli di oggi: liste 5 s (`LIST_TIMEOUT`), pagine 60 s
   (`READ_TIMEOUT`). Nessuna cache nel centrale.
5. Nel centrale, per i collegamenti `here`, si inoltrano solo JSON, download e websocket.
6. Il purge (`postrm`) non cambia: non ci sono file nuovi sulla macchina. Nel centrale si aggiungono solo `pages` per
   collegamento e la cartella del backup cifrato (F7).
7. Niente nomi del fornitore né di macchine nelle pagine del cliente: le pagine del cliente non cambiano posto.

## 9. Piano a fette (ognuna si rilascia da sola e si annulla; lab3 = centrale `full`, lab4 = agente)

Per ogni fetta: il rilascio, la prova su lab3/lab4, come si annulla, la stima.

0. **Ponte: la chiave di firma del self-update.** Carlo genera la chiave (`omniacli package-key`) e i due segreti del
   repo. Le 7 macchine si aggiornano a mano una volta, poi dal pannello.
   - *Prova*: su lab4 «Update OmniaCLI…» da una release firmata a quella dopo; un manifesto alterato viene rifiutato e
     niente si installa.
   - *Si annulla*: «Torna alla versione di prima» (14 giorni).
   - *Stima*: 0,5 g, più l'azione di Carlo.
1. **Il contratto** (`agent_api: 1`, `plugins`, la regola, la riga «pagina scritta da…»). Non cambia niente di
   visibile.
   - *Prova*: lab4 con `agent_api` 1 viene letto da lab3; lab4 con la release di prima (`dev-lab.sh release`) vale 0, e
     ogni pagina arriva da lab4 come oggi.
   - *Si annulla*: con il rilascio di prima.
   - *Stima*: 1 g.
2. **Il meccanismo e le 12 pagine A**, con `pages: here | machine` per collegamento.
   - *Prova*: su lab3 si cambia un testo di `disks.html` senza toccare lab4; `/links/lab4/disks` lo mostra; la versione
     `dpkg` di lab4 è identica prima e dopo; con `pages: machine` torna la pagina di lab4.
   - *Stima*: 2,5 g.
3. **La pagina dell'istanza** (`server.html` e 11 parziali, modello `/api/pages/server/{server}`), la più vista. Chiude
   il caso «Scarica».
   - *Prova*:
     - il pulsante «Scarica» c'è su `/links/lab4/servers/acme` con lab4 fermo alla release della fetta 1;
     - la parità del vincolo 3 per 4 ruoli dà 0 differenze;
     - nel JSON 0 segreti;
     - un plugin presente solo su lab4 riporta la pagina di lab4, con la riga che lo dice;
     - nel registro di lab4 c'è la lettura, con `on_behalf`.
   - *Stima*: 5 g.

   **Com'è nel codice (fetta 3).**
   - Il modello è `GET /api/pages/server?server=<x>&view=test` (non `/api/pages/server/{server}`: la stessa route
     delle pagine A, con i parametri in query). È un'API nuova, quindi `AGENT_API` e `CENTRAL_AGENT_API` passano a 2;
     la pagina dell'istanza chiede 2, le 12 pagine A restano a 1 e il centrale le legge ancora da una macchina a 1
     (N−1).
   - La route della pagina è divisa in due: `server_context` fa il contesto con gli oggetti vivi, come prima, e la
     pagina della macchina lo scrive così; `centralpages.server_model` lo prende campo per campo. I 49 campi del
     contesto sono in `SERVER_FIELDS`. Degli oggetti vivi (`Instance`, `Server`, `Action`, `Field`, `Job`,
     `Workspace`) c'è un elenco di campi per ciascuno; quello che il template chiama (`describe`, `db_shell`,
     `db_shell_as_service`, `backup_fields`, `window_label`) è risposto una volta, come valore: di `db_shell` solo
     sì o no, mai il comando con `PGPASSWORD`. Ogni altro oggetto è rifiutato (`ModelError`), mai scritto con il
     suo `repr`. Dell'`odoo.conf` di un'istanza di un workspace il modello dice solo se ha opzioni sue
     (`has_extra`), mai i valori.
   - Il centrale ricostruisce dal JSON oggetti che il template legge come attributi (`centralpages.server_view`) e
     scrive lo stesso `server.html` con i suoi 11 parziali. Il modello intero è rifiutato, con la pagina d'errore
     del centrale, quando:
     - nomina un dialogo di un plugin che il centrale non ha, o un nome che non è `<plugin>/<file>.html`;
     - è di un'altra istanza di quella del percorso;
     - ha un indirizzo che la pagina non può usare (`centralpages.checked_addresses`). Un link verso fuori (l'Odoo
       di un'istanza, il suo repository) è solo `http` o `https`. Un indirizzo della macchina, cioè il `url` di un
       pulsante, le richieste dei dialoghi e le pagine delle righe, sta sotto `/links/<m>/` dopo aver letto il
       percorso: niente `..`, nemmeno codificato, niente un'altra macchina, niente le route del centrale.
   - Le ore della pagina sono scritte con il fuso della macchina, che il modello dice (`server_zone`), non con quello
     del centrale. I nomi che finiscono negli script passano da `tojson`.
   - `/links/<m>/api/pages/*` non passa dal collegamento: i modelli li legge solo il centrale.
   - La parità si misura sull'intero `<main>`, non sui soli pulsanti: `tests/serverparity.py` scrive ogni pagina
     dell'istanza due volte, dal contesto vivo e dal modello passato per JSON, e le confronta. Con
     `OMNIACLI_PARITY=1` lo fa per ogni test della suite.
   - Ogni lettura di un modello è una riga `read` nel registro della macchina, con `on_behalf`, il server e
     l'esito, ogni volta (non una ogni 10 minuti come le letture sensibili): `/api/pages/{page}` passa da
     `NOT_SENSITIVE` ad `AUDITED_BY_ROUTE`.
   - Il centrale manda al modello anche `X-OmniaCLI-Tunnel`, come `link_proxy`: senza, «Entra come…» attraverso un
     tunnel del centrale risultava spento solo nella pagina scritta dal centrale (trovato sul lab, ora nel test).
   - Un'istanza spostata altrove (AD-014) va al suo host con il 307 di prima, con `?view=` tenuto. Il 307 si segue
     solo se il percorso, letto, sta sotto il collegamento. Un'istanza la cui copia lì è cancellata dà la pagina del
     suo spostamento (`moved_away.html`, classe D). Il centrale la scrive da un piccolo modello: il nome e lo
     spostamento. L'HTML della macchina non passa mai perché il modello lo dice: la prima versione di questa
     fetta (`own_page`) lo lasciava passare, e la revisione l'ha tolto.
   - La riga «aggiornala» si aggiunge solo a una pagina HTML con esito 200. Il resto passa com'è, in streaming.
   - Il ritorno di 17.2 distingue ora due casi. Una macchina che dice quello che ha sempre detto, ma meno di quanto
     chiede una pagina più nuova (una macchina a 1 per la pagina dell'istanza), mostra la sua pagina con la riga
     «aggiornala». Una macchina che dice meno di quanto aveva detto, o non ha il modello di quello che dice, ha la
     pagina del centrale che lo dice, come nelle fette 1-2.

   **Dove la fetta si discosta dal piano.**
   - «Il pulsante "Scarica" c'è con lab4 fermo alla release della fetta 1»: non si può. Il modello è calcolato
     sulla macchina (17.1), e la macchina della fetta 1 (2026.1.5515, `agent_api` 1) non ha `/api/pages/server`:
     mostra la sua pagina con la riga «aggiornala». La 5515, tra l'altro, ha già «Scarica» nel suo template. La
     prova fatta è quella che il caso chiede: lab4 con il codice di questa fetta ma con «Scarica» tolto dal suo
     `server.html`. Il centrale lo mostra, la pagina di lab4 (`pages: machine`) no.
   - Il ruolo personalizzato non passa da un collegamento: un collegamento ha il ruolo della persona del centrale,
     e una persona di un workspace non apre i collegamenti. La parità del ruolo personalizzato, e quella dello
     scope delegato di AD-014, sono provate nei test: `copy-admin` e `no-logs` sulla casa e attraverso la casa
     sull'host. Sul lab la parità è misurata per admin, developer e user.
4. **Le 14 pagine B**, in 2-3 rilasci.
   - *Prova*: la parità e lo 0 segreti, pagina per pagina.
   - *Stima*: 5,5 g.

   **Com'è nel codice (fetta 4).**
   - Un'API nuova per pagina, nella stessa route delle fette 2-3: `GET /api/pages/<pagina>`, con i nomi
     dell'indirizzo (`job_id`, `workspace`) e i filtri della pagina in query (`server`, `q`, `since`, `until`, `who`,
     `event`, `edit`, `new`). `AGENT_API` e `CENTRAL_AGENT_API` passano a 3; le 14 pagine chiedono 3, la pagina
     dell'istanza resta a 2, le 12 pagine A a 1. Il centrale legge 3 e 2 (§10.6): una macchina a 2 (2026.1.5517) ha
     dal centrale le pagine A e quella dell'istanza, e le sue pagine B con la riga «aggiornala»; una macchina a 1
     (2026.1.5515) ha tutte le pagine sue, con la riga. **Questo cambia per le macchine a 1**: fino alla fetta 3 il
     centrale scriveva loro le 12 pagine A; ora `OLDEST_READ` è 2 e le pagine A di una macchina a 1 tornano le sue,
     con la riga, finché non si aggiorna (la regola N e N−1 di §10.6, voluta).
   - L'ordine è quello di AD-018 §18.8: `account`, `jobs`, `job`, `workspaces`, `destinations` (della macchina e di un
     workspace, `workspaces/<w>/destinations`), `access` (della macchina e di un workspace, `workspaces/<w>/audit`),
     poi `config`, `credentials`, `enterprise`, `links`, `tunnels`, `groups`, `plugins`, `schedules`.
   - Ogni route è divisa in due come nella fetta 3: un contesto (`app.<pagina>_context`, registrato con `context_of`)
     che fa i controlli della route e i dati con gli oggetti vivi, scritto dalla pagina della macchina così com'è;
     `centralpages.page_model` lo prende campo per campo (`Page.fields`), i campi che la route ha messo. Gli oggetti
     vivi hanno un elenco di campi ciascuno: un server della pagina Server (`config_server_model`: stato, parole
     della ricerca del plugin, nome e ramo dei repository), un collegamento (`link_model`: dove va, il percorso SSH e
     la chiave dell'host fissata, **mai** il token), un plugin trovato, un job (`job_model`, con istanze, server,
     origine e fine; mai le righe, che lo script legge da `/api/jobs/<id>`). Ogni altro oggetto è rifiutato
     (`ModelError`).
   - Nessuno dei segreti che queste pagine gestiscono è nel modello, provato per valore e non solo per nome
     (`test_the_class_b_models_carry_no_secret_value`): il token di un collegamento della macchina, la chiave SSH
     privata, il token di una credenziale git, quello della sorgente di Enterprise, la password e la chiave di una
     destinazione (della macchina e di un workspace, anche WebDAV), la password SMTP. Delle credenziali, della
     sorgente e delle destinazioni il modello dice solo se il segreto c'è (`token_set`, `set`); della chiave SSH
     della macchina solo la metà pubblica.
   - Gli indirizzi degli avvisi (`notify`) di una pianificazione e di un workspace arrivano solo con il loro sito
     (`https://hooks.slack.com/…`): Slack e Teams portano il segreto nel percorso. Vale per la pagina della macchina
     e per il modello insieme (`notices_shown`, nel contesto), perché queste due pagine non li mostrano e non li
     rimandano al salvataggio: si cambiano nella pagina del workspace e nelle Opzioni, che restano della macchina.
   - **Cosa il modello porta com'è, perché la pagina della macchina lo mostra alla stessa persona** (la parità): i
     testi d'errore della lettura di un file (credenziali, Enterprise, archivi). Un errore di PyYAML riporta la riga
     che non legge, e quella riga può avere un token. Il modello va solo al pannello del collegamento, con il ruolo
     della persona (admin per queste pagine), come la pagina della macchina andava al suo browser. Il controllo dei nomi
     (`serverparity.secret_keys`) conosce tre nomi che non sono segreti: `token_set`, `token_people` e
     `reset_passwords` (un'opzione sì o no della copia di una pianificazione).
   - Il centrale scrive dal modello (`centralpages.page_view`) con questi controlli:
     - un nome che la pagina mette in un indirizzo (l'id di un job, il nome di un server, di un workspace, di un
       collegamento, la chiave di un'istanza `server/ruolo`) deve essere un nome: niente `..`, niente barra, niente
       spazio (`_NAMES_AT`). Un `servers[0].name = "../../links/altro/config"` porterebbe il pulsante «Modifica» su
       un'altra macchina. Dove la pagina aspetta un elenco di nomi (i job di una voce del registro, le istanze di un
       job) c'è un elenco, mai un testo che la pagina percorrerebbe lettera per lettera;
     - gli indirizzi verso fuori stanno al loro posto esatto (`_ELSEWHERE_AT`: l'indirizzo di un collegamento,
       solo `http` o `https`), non al nome di una chiave come nella fetta 3: le mappe di queste pagine hanno per
       chiave i nomi del cliente (una radice dello spazio, un token, un sottodominio chiamato `backups` o
       `preview`, l'`url` di una destinazione WebDAV), e il controllo per nome rifiutava le loro pagine;
     - il modello è della stessa pagina dell'indirizzo: lo stesso job, lo stesso workspace;
     - l'indirizzo delle richieste della pagina delle destinazioni (`api`) è un percorso della macchina;
     - nella pagina dei workspace attraverso un collegamento, le macchine dell'elenco (`links`, `machine_column`)
       sono vuote: la macchina non ne ha per un collegamento, e un nome lì costruirebbe righe e moduli sotto
       `/links/<nome>` del centrale.
     Due indirizzi sono cambiati nei template, uguali sulla macchina e qui: la riga di un collegamento nella pagina
     Collegamenti apre `{{ prefix }}/links/<nome>/` (prima `/links/<nome>/`, che attraverso un collegamento portava
     al collegamento omonimo del centrale), e lo script di un job usa `result.download` solo se è un percorso della
     macchina (niente schema, niente `//`, niente `.` o `..`) prima di offrirlo e di aprirlo da solo con
     `?download=1`.
   - I tipi di destinazione arrivano in inglese e il centrale li dice nella lingua di chi guarda, con il suo
     catalogo (come i gruppi di Impostazioni nella fetta 2).
   - `drawn_for` dice anche `workspaces_menu` (la briciola «Workspace» delle pagine di un workspace), per il ruolo di
     lì.
   - Un job che l'host di uno spostamento ha risposto attraverso la casa va all'host con il 307 di prima, come il
     log e la pagina dell'istanza.
   - La parità: `tests/serverparity.py` scrive anche ogni pagina B due volte, dal contesto vivo e dal modello passato
     per JSON (`Twice`, che con `OMNIACLI_PARITY=1` lo fa per ogni test della suite). Nei test: admin, developer e
     user attraverso il collegamento; le persone di un workspace (mario, user, developer, admin, la copia di admin
     e il ruolo personalizzato senza i log) sulla casa, prima e dopo il posizionamento di srv sull'host.
   - Sul lab è stato trovato un difetto che i test non avevano: i job della macchina stessa hanno la chiave
     `_machine/<x>`, e il controllo dei nomi li rifiutava (la pagina Job di lab4 dava 502). Ora un nome può
     cominciare con `_`, e i test hanno un job `_machine/relay`.

   **Dove la fetta si discosta dal piano.**
   - **Un rilascio, non 2-3.** Le 14 pagine chiedono tutte `agent_api` 3. Con due numeri in più (3 e 4) il centrale,
     che legge N e N−1, avrebbe smesso di scrivere le pagine della fetta 3 a una macchina a 2 già al secondo
     rilascio.
   - **`account` e `tunnels` non hanno un modello.** Sono della persona che ha fatto il login su quel pannello: la
     macchina li rifiuta a un collegamento oggi (404 «Only a user who logs in has an account», 403 «Only a user who
     logs in here may open a tunnel»). Il loro modello risponde lo stesso rifiuto, e il centrale scrive la sua
     pagina d'errore con lo stesso codice, senza mai l'HTML della macchina. Se una macchina risponde 200 con un
     contesto, il modello è rifiutato (`PERSON_PAGES`). L'account delle persone dei clienti passa nel centrale con
     AD-018: è il `/account` del centrale, non una pagina di un collegamento.
   - **Le pagine con segreti (`credentials`, `links`, `tunnels`) non vanno in classe D.** `credentials` e `links` si
     scrivono senza nessun valore segreto (provato per valore); `tunnels` è una pagina della persona (sopra).
   - **Il registro degli accessi sul lab** si confronta senza le righe e senza il conteggio: il registro cresce tra
     una richiesta e l'altra, con le letture di queste stesse pagine. La parità esatta della pagina è quella di
     `Twice`, sullo stesso contesto.
   - Come nella fetta 3, il ruolo personalizzato e le persone di un workspace non passano da un collegamento: la loro
     parità è nei test. Sul lab è misurata per admin, developer e user.
   - La pagina `workspaces` di una persona di workspace (il suo elenco, da `WORKSPACES_MENU_FROM` workspace in su)
     ha il suo modello e la sua parità nei test; i clienti la vedranno dal centrale solo dopo il loro passaggio
     (AD-018 §18.8).
5. **Le altre pagine C**: `index` (sezione macchina e scheda Database), `workspace`, `users`, `pullers`, `server_form`,
   `options`.
   - *Prova*: come la fetta 3.
   - *Stima*: 9 g.

   **Com'è nel codice (fetta 5).**
   - Sette modelli nuovi nella stessa route: `GET /api/pages/<pagina>` per `index` (la sezione della macchina, con
     le schede Monitoraggio, Database e Accesso SSH), `dashboard_servers` (la tabella delle istanze che lo script
     della sezione richiede quando un job parte o finisce: `_servers_part.html`, una parte di `index`),
     `workspace` (`workspace=<w>`), `users`, `pullers`, `server_form` (`name`, `form=edit|clone`; senza: un server
     nuovo) e `options`. `AGENT_API` e `CENTRAL_AGENT_API` passano a 4, `OLDEST_READ` a 3. Le sette chiedono 4; le
     14 B restano a 3, l'istanza a 2, le 12 A a 1.
   - **Una macchina a 3 (2026.1.5518)** ha dal centrale le 12 A, l'istanza e le 14 B; la sezione della macchina,
     la pagina di un workspace, Utenti, Puller, il modulo di un server e Opzioni sono le sue, con la riga
     «aggiornala». Una macchina a 2 o a 1 (5517, 5515) ha tutte le pagine sue, con la riga (§10.6, come nella
     fetta 4 per quelle a 1).
   - Ogni route è divisa in due come nelle fette 3-4: un contesto registrato con `context_of` (`index_context`,
     `dashboard_servers_context`, `workspace_context`, `users_context`, `pullers_context`, `server_form_context`,
     `options_context`), che fa i controlli della route e che la pagina della macchina scrive così com'è;
     `centralpages.page_model` lo prende campo per campo. Il contesto della macchina ora è già fatto di valori
     semplici: il workspace della sua pagina è `workspace_shown` (nome, etichetta, dominio, certificato,
     istanza principale, limiti, sottodomini, relay, avvisi), le Opzioni sono `OPTIONS_SHOWN` più due valori
     calcolati. Le chiavi del contesto della pagina del workspace che il template non leggeva (`parameters`, con
     i valori delle opzioni Odoo di un'istanza, `repositories`, `certificates`, `mcp_url`) sono tolte.
   - **`_machine_database.html` è JavaScript** (il «Non verificato» qui sotto): delle sue 2.115 righe legge dal
     contesto solo `prefix`; i dati li chiede a `/api/postgres` dal browser. Il suo modello è un sì o no
     (`machine_database`); per questo la fetta è costata meno della stima.
   - **I segreti, mai nel modello, provati per valore** (`test_the_models_of_slice_5_carry_no_secret_value`):
     - Opzioni: la password SMTP (il modello dice `smtp_password_set`); gli avvisi di Monitoraggio con il solo sito
       (`notices_shown`); i webhook della tabella con un nome (`ref`, sha-256 dell'indirizzo, 20 cifre), mai
       l'indirizzo: Slack e Teams tengono il segreto nel percorso, e la pagina della macchina lo metteva in
       `data-url` e nel `title`; il segreto di firma di un webhook non c'è mai stato;
     - pagina di un workspace: il segreto del provider SSO (`sso.shown`: `secret_set`); gli avvisi con il solo
       sito; dei token del workspace solo nome, ruolo e persona; delle credenziali git `token_set`;
     - Utenti e persone di un workspace: mai un hash, un segreto TOTP, un codice di recupero, una passkey, una
       sessione: se il secondo fattore è attivo, quante passkey e quante sessioni;
     - Puller: di una pubblicazione e di una connessione se ha la chiave (`has_key`);
     - modulo di un server: la configurazione del server com'è nel file (un server non ha password: quella del
       database si legge dal suo `odoo.conf`);
     - sezione della macchina: il cluster, le repliche e le loro password stanno dietro `/api/postgres`, non nel
       modello.
   - **Avvisi mostrati con il solo sito, tenuti al salvataggio** (`notices_kept`). La pagina di un workspace e le
     Opzioni mostrano un webhook come `https://hooks.slack.com/…` anche nel campo da modificare, sulla pagina della
     macchina e qui (la parità). Al salvataggio, una riga uguale al sito di un webhook salvato vale quel webhook,
     nell'ordine; una che non corrisponde a nessuno è rifiutata («scrivi il suo indirizzo intero»). Le azioni dei
     webhook di Opzioni (firma, togli la firma, job, prova) li nominano con `ref`; `url` resta accettato per uno
     script di prima.
   - Il centrale scrive dal modello (`centralpages.page_view`) con questi controlli, oltre a quelli della fetta 4:
     - nomi al loro posto (`_NAMES_AT`): le righe delle istanze (nome e genitore, anche nei gruppi a ogni
       profondità), i job, i workspace del carico; le persone, i server, le istanze, i collegamenti e i token di
       un workspace; le persone, i loro workspace, i collegamenti e i token di Utenti; puller, connessioni,
       pubblicazioni e collegamenti di Puller; il server e i collegamenti del modulo;
     - indirizzi verso fuori al loro posto (`_ELSEWHERE_AT`): l'indirizzo del portale di Utenti e di Opzioni;
     - il logo della macchina in Opzioni (`machine_logo`) è `/brand/logo?v=…` sotto il prefisso, o niente;
     - **campi forzati dal centrale, mai la parola della macchina**: delle righe delle istanze `base` (il prefisso
       del collegamento) e `link` (vuoto); attraverso un collegamento, i Collegati della sezione
       (`machine_links`, `linked_sections`) sono vuoti, come la macchina li dà a un collegamento; del modulo di un
       server i tipi, con i campi e i pulsanti dei plugin (`centralpages.plugin_types`, la stessa funzione che usa
       la macchina): un `route` di un pulsante viene preso dallo script del modulo e aperto;
     - il modello è della pagina dell'indirizzo: lo stesso workspace; per il modulo di un server lo stesso
       server, il titolo atteso (`New server`, `Edit <s>`, `Clone of <s>`) e il nome del clone `<s>_copy…`.
   - La sezione della macchina dice la versione di OmniaCLI **della macchina** (`machine_version`), non quella del
     pannello che la scrive; le Opzioni il logo della macchina, non quello del centrale.
   - Ogni pagina scritta qui (anche delle fette 1-4) dice nella risposta il fuso della macchina
     (`X-OmniaCLI-Zone`), come la sua pagina: prima diceva quello del centrale, mentre le ore erano già scritte con
     quello della macchina (trovato da `test_clock` sulla sezione della macchina, che prima arrivava dalla macchina).
   - La parità: `tests/serverparity.py` scrive anche ognuna delle sette due volte (`Twice`, per ogni test della
     suite con `OMNIACLI_PARITY=1`); una parte di pagina senza `<main>` si confronta intera. Nei test: admin,
     developer e user attraverso il collegamento; le persone di un workspace (mario, user, developer, admin, la
     copia di admin e il ruolo personalizzato senza i log) sulla sezione e sulla pagina del workspace, prima e
     dopo il posizionamento di srv sull'host.
   - **Utenti e Opzioni attraverso un collegamento** (la scelta chiesta): si scrivono. Non sono della persona che ha
     fatto il login (come Account e Tunnel): sono le persone della macchina e dei suoi workspace e le opzioni della
     macchina, che il suo admin cura. Nessun segreto passa (sopra). AD-018 porterà le identità dei clienti nel
     centrale: allora Utenti di una macchina avrà solo il suo personale, e la pagina resta scritta qui.

   - **Sul lab** (lab3 centrale, lab4 macchina, 24/11/2026): con il codice della fetta su tutte e due, le 11 pagine
     provate (la sezione con e senza ricerca, la tabella, due workspace, Utenti, Puller, i tre moduli di un server,
     Opzioni) e le 9 delle fette 1-4 sono scritte da lab3; parità per admin, developer e user: 33 confronti, 0
     differenze; nei 18 modelli letti su lab4 i 46 valori segreti che lab4 tiene (token dei collegamenti, hash di
     persone e token, segreti TOTP e codici di recupero, sessioni, segreto SSO, password SMTP e segreto di firma di un
     webhook, percorsi dei webhook, chiavi dei puller, chiavi private e store git, la password della replica): 0
     trovati. Il giro con Playwright (italiano, 1440 e 375, chiaro e scuro): 0 errori di pagina, 0 dialoghi del
     browser, 0 risposte 5xx; aperte le quattro schede della sezione e 80 finestre (nulla inviato). Con lab4 alla
     2026.1.5518 (`agent_api` 3): le 9 pagine delle fette 1-4 scritte da lab3, le 10 pagine della fetta 5 di lab4 con
     la riga, la tabella delle istanze di lab4 senza la riga. La riga dentro la tabella, a ogni job, l'ha trovata il
     lab: ora `with_line` lascia com'è una parte di pagina senza `<body>`.
   - Le righe della scheda Database con i loro pulsanti (proposta, replica verso, archivio) non si sono potute aprire
     sul lab: i cluster di lab4 erano fermi. Si è aperta «Replica di un'altra macchina…». Il loro script è quello di
     prima, e dal modello legge solo il prefisso.

   **Dove la fetta si discosta dal piano.**
   - **Una pagina in più, `dashboard_servers`.** Lo script della sezione richiede la tabella delle istanze come HTML
     (`/dashboard/servers`): scritta dalla macchina, sarebbe passata sotto l'origine del centrale proprio sulla
     pagina che il centrale scrive. Ora la scrive il centrale, dallo stesso tipo di modello.
   - **La pagina della macchina cambia in tre punti, per la parità e per i segreti**: gli avvisi di un workspace e
     di Monitoraggio si vedono con il solo sito anche nel campo da modificare (un webhook si cambia riscrivendolo
     intero, e una riga lasciata com'è lo tiene); i webhook di Opzioni non hanno più l'indirizzo nella pagina; la
     sezione legge `machine_version` invece della versione globale.
   - **Restano HTML della macchina** l'invito (classe D) e le pagine proprie di un plugin sotto `/x/` (i dialoghi
     dei plugin del pacchetto sono già scritti qui con l'istanza): sono della fetta 6, che inoltra solo JSON.
6. **La modalità `agent`**: la scritta in testa, l'admin locale obbligatorio, il centrale che inoltra solo JSON per i
   collegamenti `here`.
   - *Prova*:
     - lab4 in `agent`: una risposta `text/html` di una pagina da lab4 viene rifiutata da lab3 (salvo il ritorno);
     - la riserva via `ssh -L` funziona con l'admin locale;
     - il passaggio a `agent` senza un admin locale viene rifiutato;
     - con lab3 spento Odoo risponde su lab4 e un backup pianificato parte all'ora.
   - *Si annulla*: con `panel: full`.
   - *Stima*: 2 g.
7. **La protezione del centrale**: il backup cifrato notturno e il ripristino.
   - *Prova*: il ripristino su un lab nuovo apre lab4 (tempo misurato); il backup non si legge senza la passphrase.
   - *Stima*: 1,5 g.
8. **L'agente dietro NAT via relay** (17.6).
   - *Prova*: lab4 dietro il NAT simulato di AD-008 §9 passo 7, nessuna regola nuova in entrata; il collegamento SSH al
     relay porta le pagine della fetta 3; il tunnel ucciso torna entro 35 s.
   - *Stima*: 3 g.

Totale ≈ 30 giornate. Le fette 0-3 (9 g) bastano per il caso di oggi e per la pagina che Carlo usa di più.

## 10. Da confermare con Carlo (la raccomandata per prima)

1. **Dove vive il centrale.**
   - Raccomandata: **resta sul PC di Carlo** (`localhost:8090`). Nessuna porta nuova; tiene i 7 token e le chiavi
     SSH; 17 MB da salvare ogni notte. Col PC spento: Odoo, backup e portali vanno, le pagine si aprono con la riserva
     (`ssh -L`) macchina per macchina.
   - Alternativa: un VPS. Lo raggiungono anche i colleghi, ma una macchina pubblica tiene le chiavi di tutte e 7:
     servono il secondo fattore obbligatorio, il firewall e i suoi aggiornamenti.
2. **Il portale dei clienti.**
   - Raccomandata: **resta sulle macchine** (17.4.1). Esempio: il pulsante «Scarica» arriva a Carlo con la fetta 3 sulle
     macchine a `agent_api` 1. A un cliente che guarda i suoi backup dal portale arriva quando si aggiorna la sua
     macchina.
   - Alternativa: portale nel centrale. Chiede il centrale pubblico e sempre acceso, il DNS dei 5 portali dei clienti
     sul centrale, gli utenti dei workspace spostati, e un portale fermo per tutti i clienti insieme quando il centrale
     è giù. È un AD a sé.
3. **Da cosa gira il centrale.**
   - Raccomandata: **da una release**, aggiornato a mano come la produzione. Da quando scrive le pagine di 7 macchine, una
     pagina rotta nel working tree è rotta per 7 macchine. Dal 17/11 i commit sono stati 233, e il centrale li
     seguirebbe tutti.
   - Alternativa: dal working tree come oggi, con `pages: machine` come freno a mano per collegamento.
4. **Il secondo fattore per gli utenti del centrale.**
   - Raccomandata: **obbligatorio se il centrale ascolta oltre `localhost`**, facoltativo su `localhost`.
   - Alternativa: sempre obbligatorio. Su `localhost` è una passkey a ogni sessione.
5. **Il centrale di riserva.**
   - Raccomandata: **solo il backup cifrato** e il ripristino provato (obiettivo ≤ 15 min).
   - Alternativa: un secondo centrale acceso, con token suoi su ogni macchina. Sono 7 token in più da tenere e
     revocare.
6. **Quante versioni di agente regge il centrale.**
   - Raccomandata: **N e N−1**. Una macchina due `agent_api` indietro mostra la sua pagina con la riga «aggiornala».
   - Alternativa: tutte dalla 1. Ogni pagina tiene un ramo per ogni versione, e il test di contratto cresce di una
     fixture per versione.

### Le risposte di Carlo (24/11/2026)

1. **Dove vive il centrale:** su un VPS, una macchina esterna. Per lo sviluppo resta per ora sul suo PC
   (`localhost:8090`). Valgono quindi i vincoli dell'alternativa: secondo fattore obbligatorio (il centrale ascolta
   oltre `localhost`), firewall e aggiornamenti del VPS. La fetta del trasloco sul VPS va aggiunta al piano di §9.
2. **Il portale dei clienti:** **nel centrale**, contro la raccomandata. 17.4.1 cambia: il centrale diventa la casa
   degli utenti dei workspace, e serve un AD a sé (AD-018) per tre cose: come gli utenti, gli inviti, il secondo
   fattore e le passkey passano dalle macchine al centrale; il DNS dei portali dei clienti; cosa vede un cliente quando
   il centrale è fermo (Odoo continua a girare). Finché AD-018 non è deciso, le fette 0-5 non toccano il portale.
3. **Da cosa gira il centrale:** da una release, aggiornato a mano (la raccomandata).
4. **Secondo fattore e riserva:** obbligatorio fuori da `localhost`; la riserva è il backup cifrato con il ripristino
   provato (la raccomandata). Sul VPS il secondo fattore è quindi sempre obbligatorio.
5. **Versioni dell'agente lette dal centrale** (§10.6, non chiesta): N e N−1, la raccomandata, salvo obiezione.

## 11. Alternative scartate

- **B. Tutto com'è più il self-update firmato.** Ogni correzione di pagina vuol dire 7 aggiornamenti, ciascuno con ≈ 20 s
  di pannello assente e il nome scritto sulle macchine di produzione. Nella settimana misurata, 42 commit su 233 erano di
  sola pagina. La chiave di firma si fa comunque, come ponte (fetta 0).
- **C. Una SPA statica servita dal centrale.** Vuol dire riscrivere in JS 25.843 righe di Jinja e i testi di `_()` (592
  solo in `server.html`) in un catalogo JS. È il costo più alto per lo stesso risultato di A.
- **D. Il centrale che spinge i template alle macchine.** È codice non firmato che arriva in produzione fuori dal
  pacchetto e fuori dall'aggiornamento a mano. In più i template nuovi incontrerebbero il contesto Python vecchio della
  macchina.
- **E. Un agente in un pacchetto separato** (o in un repo separato). Il cliente che riceve un workspace deve avere il
  pannello completo, quindi i pacchetti diventano due da costruire, firmare e provare. La modalità di 17.3 dà la stessa
  separazione con un'impostazione.
- **F. Tutto nel centrale, anche identità e portale.** Romperebbe il modello casa/host di AD-014, e senza centrale
  verrebbero meno il portale e i token dei clienti. Resta aperto solo come §10.2, alternativa.

## Non verificato

- Che ogni macchina abbia oggi un admin locale con il secondo fattore: chi arriva da un altro pannello è admin di lì
  (`a_machine(me)`), quindi potrebbe non esserci nessuno di locale. Si legge alla fetta 6.
- I plugin di terzi installati sulle macchine (`pluginstore.py`): non contati. Dicono quante pagine resterebbero della
  macchina per la regola di 17.2.
- La quota di commit «solo pagina» in una settimana senza helper nuovi: misurata solo dal 17/11, durante la costruzione
  di AD-008.
- ~~Che `_machine_database.html` (2.115 righe, 565 `{{`, 1 `fetch`) sia soprattutto JS~~: verificato nella fetta 5. È
  JavaScript: dal contesto legge solo `prefix` (le 565 `{{` sono testi `_()`), i dati vengono da `/api/postgres`.
- Che `ssh -R` sul relay regga due porte per macchina (nomi di Odoo e agente) senza cambiare `relay-hosted.json`: il
  formato è letto, la prova no.
- Il tempo del ripristino del centrale: nessuna misura.
- Che ogni azione delle pagine di classe C abbia già un gemello in `/api/v1` e MCP: la regola vale per le funzioni nuove,
  e la parità di quelle esistenti non l'ho contata.

## Tracciabilità

Chat di Carlo del 24/11/2026 («ha senso o è meglio concentrare tutto?», sì all'AD). Vincoli da AD-008 §8, AD-014 (casa,
intestazione d'ambito, due controlli) e AD-015. Nessun `TS-NNN`, `T<n>`/`HT<n>`.
