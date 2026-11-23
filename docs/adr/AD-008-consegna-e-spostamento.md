# AD-008…AD-015 — Consegnare un workspace al cliente, e spostare un'istanza fra le nostre macchine

Stato: **proposta del 23/11/2026, da accettare**. Serve la build di `docs/ux/UX-014-consegna-e-spostamento.md`
(parte A, la consegna; parte B, lo spostamento), con le decisioni di Carlo del 23/11: D-A3 copia ferma e backup per 7
giorni, poi cancellati; D-A4 il passaggio lo preme il cliente; D-A5 passano gli hash delle password e i segreti TOTP;
D-B2 l'istanza spostata resta gestita dalla casa del suo workspace. Le altre decisioni di UX-014 seguono la
raccomandata.

Codice letto alla revisione `ca95d90` (`carlo_dev_2026`). **Prerequisito**: la pagina di manutenzione che UX-014 §0 dà
per esistente (`dcf5cab`, `workspace-helper maintenance … on|off`, `ops.while_stopped`) **non è su `carlo_dev_2026`**: è
solo sul ramo `pending-stop-during-update`. Va unita prima del passo 4 di §9.

Le regole di sempre valgono e non si ripetono: chi ha i dati del cliente li tiene sul disco del suo workspace
(AD-002); ogni azione è un job nel registro degli accessi di tutte e due le macchine (UX-010 §2 r.4); un segreto
passa al helper sullo standard input, mai negli argomenti (`owncert`); un dato del fornitore non compare dalla parte
del cliente (`customerview.py`).

Origine: Carlo, 23/11/2026, in chat («spostare i workspace», «lui si installa il proprio ambiente e recupera le
informazioni sul workspace da questo», «passare le istanze da un server all'altro»). Nessun `TS-NNN`, `T<n>`/`HT<n>`.

## 0. Il costo, in numeri (l'esempio di UX-014: Cliente1)

Cliente1: `cliente1-erp` (database dedicato 1,8 GB, file 1,2 GB, codice 0,3 GB), `cliente1-shop` (cluster della
macchina, 0,5 GB), 6 utenti (3 con TOTP, 1 con passkey), ultimi backup 2,4 GB, 57 GB di backup vecchi, linea in
uscita 22 MB/s. Misurato vuol dire misurato; il resto è stimato e va misurato al passo di §9 indicato.

| Cosa | Quanto | Fonte |
|---|---|---|
| Ricevi (con le istanze che girano) | 6,1 GB a 22 MB/s ≈ 4 min 40 s | stima, §9 passo 7 |
| Porta del database aperta da noi durante Ricevi | 1 porta, 1 indirizzo /32, al più le 72 h del codice | AD-010 |
| Fermo di un database dedicato al passaggio | ≈ 10 s di database (il ritorno di UX-011, misurato il 22/11) + avvio di Odoo + controlli | avvio non misurato, §9 passo 4 |
| Fermo di un database del cluster | dump + trasferimento + restore: 0,5 GB ≈ 3 min, 1,8 GB ≈ 8 min a 22 MB/s | stima, §9 passo 4 |
| Fermo dell'intera consegna | quello dell'istanza più lenta (le istanze passano in parallelo, 2 alla volta) | AD-011 |
| Disco da noi dopo il passaggio | 4,1 GB del workspace + 57 GB di backup, 7 giorni | D-A3 |
| Disco in più da noi per preparare | 0 per i file (letti dove sono); i dump del cluster solo al passaggio | AD-010 |
| Traffico del cliente che passa da noi decifrato dopo il passaggio | fino a 7 giorni, finché il DNS non punta da lui | AD-012 |
| Spostamento B di `acme-erp` su linea locale 60 MB/s | 3,3 GB ≈ 1 min, più i suoi backup locali (dimensione da leggere sull'istanza) | stima, §9 passo 4 |

---

## AD-008 — Il canale della consegna: la macchina del cliente chiama la nostra in HTTPS, con un codice

**Contesto.** Il cliente non ha un collegamento con noi e non apre porte per ricevere (UX-014 A2.2, D-A2): la sua
macchina può stare dietro NAT (lab4). `identify()` in `app.py` riconosce solo tre credenziali: la sessione del
modulo di login, il token di un collegamento, il token MCP/API; un token di collegamento raggiunge ogni route al
suo ruolo. Esistono già: biglietti di caricamento a uso singolo e a tempo (`uploads.py`), hash dei token
(`auth.hash_token`, `sha256$…`), AES-256-GCM e HKDF di `cryptography` (`pullcrypto.py`), il trasferimento di un file
intero attraverso un collegamento (`linkcopy.upload`/`_download`: un solo POST o GET, nessuna ripresa, 300 s per
passo).

**Opzioni considerate.**
- A. **La macchina del cliente chiama la nostra** in HTTPS, con un codice di consegna che la autorizza a leggere un
  solo pacchetto e a chiedere un solo passaggio.
- B. Un file del pacchetto scaricato e caricato: niente rete fra le due, ma il fermo è tutto il viaggio (≈ 15 min per
  Cliente1). UX-014 lo mette in DEFER.
- C. Un collegamento temporaneo fra le due macchine: lascia un legame che il cliente non vuole e il token di un
  collegamento apre ogni route al suo ruolo, non un pacchetto.
- D. Noi spingiamo verso il cliente: gli serve una porta aperta in entrata (vietato da A2.2).

**Decisione.** A, con un'API sua, `/api/handover/v1/…`, fuori da `identify()`: la autorizzano solo le credenziali della
consegna (il codice, poi la sessione nata dal codice).

**Motivazione.** È l'unico verso che non chiede porte al cliente. Un'API con le sue credenziali non può allargarsi a
una route qualunque: il codice legge un pacchetto, non la macchina.

**Il codice.** Una riga, `omniacli-consegna-1.<base64url JSON>` con `u` (l'indirizzo pubblico del nostro pannello:
`portal_address`, o un nome con certificato), `h` (l'id della consegna, 16 esadecimali), `s` (32 byte casuali), `p`
(SHA-256 della chiave pubblica, SPKI, del certificato che `u` presenta oggi). Da noi resta solo `sha256$` di `s`.
- **72 h** dalla creazione. Scaduto: `offer`, `claim`, la copia e l'inizio del passaggio sono rifiutati. Un passaggio
  già iniziato si chiude (`commit` o `abort`) anche oltre le 72 h, fino alla sua scadenza (AD-011).
- **Un solo workspace**: l'id lega il codice a un workspace e a una scelta di cosa passa, fissata in Prepara.
- **Una sola macchina** (uso singolo): `POST /claim` con il codice e la chiave pubblica X25519 che la macchina del
  cliente genera per questa consegna. Il primo claim lega la consegna a quella chiave e restituisce una **sessione**
  (32 byte, da noi solo l'hash), il credenziale di tutte le richieste successive. Un secondo claim con un'altra chiave
  riceve 409, «già usato da un'altra macchina». Lo stesso claim ripetuto con la stessa chiave dà una sessione nuova
  (la risposta di prima si era persa).
- **Revocabile**: Annulla la consegna cancella gli hash del codice e della sessione. Rifai il codice fa un codice
  nuovo e cancella la sessione di prima; la macchina che aveva già copiato reclama il codice nuovo e riprende da dove
  era (AD-010), una macchina diversa ricomincia.
- **Rifiuti**: un id o un segreto sbagliati danno sempre lo stesso 401 («codice non valido»). Solo con il segreto
  giusto si legge il motivo vero (scaduto, usato, annullato), che §A11 deve poter dire. I tentativi falliti vanno nel
  file degli accessi falliti di `accesslog.py`, con i suoi limiti per minuto. Oltre 10 tentativi falliti in 10 minuti
  dallo stesso indirizzo: 429.

**Le route da noi** (fornitore; `machine_admin`, fuori da `WORKSPACE_ROUTES`):
- `GET|POST /api/workspaces/{ws}/handover`: lo stato e Prepara (con il nome scritto e la scelta). Il codice si mostra
  una volta sola;
- `POST /api/workspaces/{ws}/handover/code`: Rifai il codice;
- `POST …/handover/cancel`, `…/handover/delete-now`, `…/handover/reactivate`: quest'ultima con il nome scritto.

**Le route della consegna** (pubbliche, autorizzate dal codice o dalla sessione):
- `GET  /api/handover/v1/offer`, con il codice: in sola lettura, cosa arriva e cosa serve (§A5 passo 1);
- `POST /api/handover/v1/claim`;
- `GET  /api/handover/v1/{id}/manifest?round=n`;
- `GET  /api/handover/v1/{id}/files/{item}/{path}`, con `Range`;
- `POST /api/handover/v1/{id}/trees/{item}`: nel corpo un elenco di percorsi, la risposta è un flusso tar (AD-010);
- `GET  /api/handover/v1/{id}/secrets`, sigillato (AD-009);
- `POST /api/handover/v1/{id}/db/{instance}/allow` e `GET …/db/{instance}/state`;
- `POST /api/handover/v1/{id}/probe`: noi proviamo da fuori le sue porte 80 e 443;
- `POST /api/handover/v1/{id}/switch/begin|commit|abort` e `GET …/switch` (AD-011);
- `POST /api/handover/v1/{id}/cancel`, che arriva da Annulla la ricezione;
- `GET  /api/handover/v1/{id}/state`.

**Dalla macchina del cliente** (`admin` della sua macchina):
- `POST /api/handover/read`: legge `offer` per il dialogo. Il browser non chiama la nostra origine;
- `POST /api/handover/receive`, `…/{id}/retry`, `…/{id}/switch` (con il nome scritto), `…/{id}/cancel`, `GET …/{id}`;
- REST e MCP, come chiede UX-014 A13: `prepare_handover` da noi, `receive_workspace` e `switch_workspace` (con
  `confirm` = nome) da lui.

**Contratto d'invarianti** (quello che il canale presume):

| Invariante | Vale? |
|---|---|
| Il nostro pannello è raggiungibile da fuori in HTTPS con un certificato valido | **Non sempre**: lab1 e lab2 sono su indirizzi privati (UX-014, Non verificato). Senza `portal_address` né un nome con certificato, Prepara è spento prima del passo 1 (§A11) |
| Il certificato dietro `u` resta lo stesso per 72 h (per `p`) | **Quasi sempre**: certbot rinnova 30 giorni prima della scadenza e di default genera una chiave nuova. Un rinnovo nella finestra fa fallire il controllo, con «il certificato del fornitore non è quello del codice: chiedi un codice nuovo» (Rifai) |
| L'indirizzo da cui il cliente chiama è quello da cui arriverà la sua replica | Sì dietro un NAT semplice; **no** con più uscite. Il primo `allow` prova la connessione e, se fallisce, il database passa per dump (AD-010) |
| Dietro il nostro nginx, `request.client.host` è l'indirizzo vero | **Sì**: uvicorn con `proxy_headers=True` e `forwarded_allow_ips="127.0.0.1,::1"` (`cli.py` righe 22-40) crede a `X-Forwarded-For` solo dal nginx della macchina |

**Conseguenze (vincoli).**
1. Il controllo del certificato del cliente è doppio: la verifica normale delle CA di sistema **e** il confronto con
   `p`. Un proxy aziendale che intercetta il TLS viene rifiutato, ed è voluto.
2. Fra il nostro nginx e il pannello: `proxy_buffering off` sui download, `client_max_body_size` per gli elenchi,
   `proxy_read_timeout` di almeno 300 s. Il README lo scrive come fa per il relay.
3. Al massimo 4 richieste di copia alla volta per consegna; nessun limite di banda nella prima versione (si misura,
   §9 passo 7).
4. Il claim è un evento a sé: va nel registro degli accessi, nella campanella degli admin della macchina e nella banner
   («ricevuta dalla macchina 203.0.113.40, impronta ab12…»). L'impronta è nel dialogo del cliente: chi vuole la
   confronta al telefono.

**Tracciabilità.** UX-014 A2.2, A4 passo 3, A5, A11, A13; D-A2.

## AD-009 — Il pacchetto: un manifesto, dei file e i segreti sigillati; cosa passa e cosa no

**Contesto.** `instancearchive.py` fa un pacchetto di **un'istanza** (zip con manifesto SHA-256, `repositories.json`
senza utente né token, `code/`, `config/`), non del workspace. Dove sta oggi quello che passa:
- in `/var/lib/omniacli`: `workspaces.yaml` (anche ruoli, `member_roles`, `member_servers`, `token_people`, `aliases`,
  `redirects`), `users.yaml` (hash `pbkdf2_sha256`), `second-factor.json` (segreto, `last`, recovery salati),
  `passkeys.json`, `sessions.json`, `oauth.json`, `schedules.yaml`, `destinations.yaml` (600), `credentials.yaml` e
  `credentials/`, `deploy-hooks.json`, `github-hooks.json`, `deploys/`, `module-uploads/`, `enterprise.yaml`;
- di root: il certificato del cliente in `/etc/omniacli/certs/<ws>/`, il codice di Odoo Enterprise in
  `/var/lib/omniacli-enterprise/<serie>`;
- sul disco del workspace: progetti, filestore e repository.

**Opzioni considerate.**
- A. Un solo archivio (un `instancearchive` del workspace) fatto e poi scaricato: raddoppia il disco da noi (6,1 GB)
  e una ripresa ricomincia da capo.
- B. **Un manifesto versionato, dei file letti dove sono e i segreti in un blocco sigillato** a parte.
- C. Copiare i file di configurazione così come sono: porterebbero percorsi, porte e nomi nostri (smell di UX-014) e
  gli utenti del fornitore.

**Decisione.** B.

**Il formato** (`format: 1`): `GET …/manifest?round=n` dà:
- `provider` (`provider_label`, altrimenti «il fornitore»);
- `workspace` (nome, label, dominio, `disk_gb`, l'usato, cpu, memoria);
- `requirements`: il protocollo 1, la versione minima di OmniaCLI, ogni template con versione e SHA-256, la serie di
  Enterprise, il major di PostgreSQL, l'architettura (`uname -m`), il fuso della macchina;
- `items`, uno per cosa scelta;
- `trees`, gli elenchi dei file con dimensione e hash;
- `secrets`, solo la dimensione.

Nomi logici e percorsi relativi, mai un percorso, una porta o un nome di macchina nostri. Chi riceve non si fida del
contenuto, come `instancearchive`: ogni nome e ogni percorso si validano, si scrive solo dentro le cartelle sue e ogni
hash si ricalcola.

| Passa (D-A5…A8) | Da dove | Come arriva da lui |
|---|---|---|
| Il workspace | la sua voce in `workspaces.yaml` | un workspace nuovo, con lo stesso nome o con `<nome>-2`; lo storage lo sceglie lui |
| Le istanze scelte, produzione e test | `instance.json` e le opzioni di Odoo | rifatte con il suo `create` (sue porte, subnet e slice). `odoo.conf` lo riscrive `write_conf`: non si copia |
| Database e filestore | AD-010 | AD-010 |
| I repository al loro commit, con la storia | `addons/<repo>`, `.git` compreso (i commit che il remoto non ha più passano), l'indirizzo senza utente (`_USERINFO`) | albero di file |
| I moduli caricati (OPL-1, con la spunta) | `module-uploads/` | file |
| Odoo Enterprise, solo con «la sottoscrizione è del cliente» | `/var/lib/omniacli-enterprise/<serie>` | albero di file, installato dal suo `enterprise-sync`. Senza la spunta, Controlli chiede la sua configurazione di Enterprise prima di Passa |
| Gli utenti `workspace` membri di W, con l'hash della password | `users.yaml` | utenti suoi; un nome che c'è già entra come `<nome>.<ws>`, e il dialogo lo dice (UX da completare) |
| TOTP: segreto, `last` e recovery salati | `second-factor.json` | sigillato |
| Ruoli personalizzati, ruoli e istanze delle persone | `workspaces.yaml` | riscritti con la mappa dei nomi |
| Nomi, sottodomini, indirizzi in più, rinvii | `workspaces.yaml` | il suo `web` |
| Il certificato del cliente con la chiave | root (`owncert-read`, nuovo) | sigillato, poi il suo `owncert` sullo standard input |
| Le pianificazioni delle istanze di W, con il fuso della sorgente | `schedules.yaml` | la conversione la fissa il dev-lead su §A11 |
| Le destinazioni di W con le chiavi | `destinations.yaml` | sigillato |
| Le credenziali git di W e dei suoi server | `credentials.yaml` | sigillato |
| Lo stato della CI | `deploy-hooks.json` | solo acceso o spento: il segreto e il webhook li rifà la sua macchina con il token GitHub (§A4) |
| Lo storico dei deploy | `deploys/` | file |
| L'ultimo backup di ogni ambiente (D-A7) | `backups/<server>/<role>/` | file, con SHA-256 |
| L'estratto del registro degli accessi (D-A8) | le righe con `workspace` = W o W in `workspaces` | un file JSONL nella cartella del workspace ricevuto (non nel suo registro: le sue catene restano intere) |

**Non passano**: staging, passkey, sessioni, inviti, token MCP/API, accessi OAuth, il marchio, lo storico dei job, il
nostro registro, il carico misurato, le repliche, la pubblicazione via relay, le destinazioni della macchina, i
certificati Let's Encrypt. Il manifesto li elenca **per nome**, per il «non passa» con il perché.

**I segreti, sigillati.** `GET …/secrets` restituisce un JSON con gli hash degli utenti, il TOTP, le destinazioni, le
credenziali e il certificato del cliente, cifrato al momento della richiesta: X25519 fra una chiave effimera nostra e
la chiave del claim, HKDF-SHA256, AES-256-GCM, con id della consegna e round come dati associati. Da noi non si
scrive mai su disco. Da lui resta sigillato in `handover/in/<id>/secrets.sealed` fino all'import, che avviene al
passaggio con l'ultimo round (gli utenti possono cambiare password nei giorni della copia). Dopo l'import si
cancellano il file e la chiave privata del claim.

**L'estratto del registro.** Le righe passano con la redazione di `customerview.py` (le persone del fornitore
diventano `provider`, le altre macchine «un'altra macchina», le cartelle `…/<ultima parte>`). Ogni riga porta `orig`,
lo SHA-256 della riga originale, e in testa ci sono il periodo e l'esito di `AccessLog.verify()` al momento
dell'estrazione: noi possiamo dimostrare a richiesta che ogni riga è nel nostro registro.

**Contratto d'invarianti** (riusare l'identità del nostro pannello su un altro):

| Invariante | Vale? |
|---|---|
| L'hash della password si verifica con lo stesso codice | **Sì**: `pbkdf2_sha256`, con le iterazioni nell'hash (`auth.hash_password`), stesso OmniaCLI |
| Il segreto TOTP vale senza lo stato di chi lo usa | **No**: `last` (il passo dell'ultimo codice) impedisce di riusare un codice; passa con il segreto, altrimenti un codice letto alle 22:03 da noi vale di nuovo da lui nello stesso passo di 30 s |
| Una passkey vale su un altro indirizzo | **No**: è legata al RP ID, cioè il nome del pannello (`passkeys.rp_of`). Non passa |
| Un accesso OAuth vale su un'altra risorsa | **No**: è legato all'URL MCP (RFC 8707, `oauth_audience`). Non passa |
| Un token vale su un altro pannello | **Sì**, ed è il motivo per cui non passa (D-A6): un token nato per noi aprirebbe il suo |
| I nomi sono unici dalla parte di chi riceve | **No**: il workspace, i server `<ws>-<istanza>` e gli utenti possono esserci già. Mappa dei nomi obbligatoria, applicata a ogni riferimento (ruoli, `member_servers`, pianificazioni, destinazioni legate, deploy) |

**Conseguenze (vincoli).**
1. Il pacchetto non porta niente che indichi una nostra macchina (smell di UX-014). Lo prova un test: `grep` del
   manifesto, dei JSON e dei log del job di lab4 per il nome, l'IP e i percorsi di lab3 (criterio 6).
2. Gli utenti del fornitore membri di W (Carlo, i colleghi) non passano: passa solo chi ha il ruolo `workspace`.
3. Da noi, al commit, i token di W smettono di valere: puntano a una copia ferma.

**Tracciabilità.** UX-014 A4 passo 1, A9, D-A5…D-A8, criteri 6, 8, 9.

## AD-010 — Un solo motore di copia per A e B: alberi di file a pezzi che riprendono, e il database dove l'ha PostgreSQL

**Contesto.** Oggi:
- `linkcopy` porta un file intero, senza ripresa;
- `instancearchive` fa uno zip;
- `managedreplica` sa seguire il database dedicato di un'istanza su un'altra macchina (`Dedicated`, `allow`: porta
  pubblicata da `compose.override.yaml`, `pg_hba` per un /32, firewall) e sa farlo tornare **nella cartella dati
  dell'istanza, sul disco del suo workspace**, seguendolo in un progetto a parte `.replica/return` fino a Completa
  (`DedicatedBack`, `back_prepare/copy/start/wait/promote`, `return_stop`: misurato 10 s senza Odoo);
- AD-007 sposta un database con dump e restore.

Il filestore sta in `<progetto>/data/filestore`, montato nel container di Odoo; i repository in `<progetto>/addons`,
dove OmniaCLI fa già git.

**Opzioni considerate.**
- A. Un archivio intero, poi scaricato (AD-009 A).
- B. rsync via SSH: chiede account e chiavi SSH fra noi e il cliente (un legame) e una porta SSH in entrata da una
  delle due parti.
- C. **`treesync`**: manifesto, giri, file a intervalli (`Range`) o a lotti tar. Per il database, la replica di
  PostgreSQL dove si può e il dump dove no.

**Decisione.** C, in un modulo `omniacli/treesync.py` con due guidatori:
- **pull**: chi riceve chiede (A, e il ritorno di B);
- **push**: la casa spinge attraverso il suo collegamento (lo spostamento di B).

Per il database dedicato, un posto d'arrivo `ArrivePlace` in `managedreplica.py` che generalizza `DedicatedBack`.

**Come copia `treesync`.**
- **Giri.** Il giro 0 copia tutto; ogni giro dopo è la differenza fra due manifesti (nuovi, cambiati, tolti). L'ultimo
  si fa a sorgente ferma, al passaggio. «Al passo», per i file: l'ultimo giro finito da 10 minuti al massimo.
- **Due classi di file.**
  - I file che OmniaCLI legge (backup, dump, moduli caricati, deploy, repository) vanno a intervalli: `GET` con
    `Range`, ripresi dalla dimensione del `.part`.
  - Gli alberi che legge solo root (il filestore) vanno a lotti di ≤ 256 MiB. L'elenco dei percorsi va al helper
    `tree-read`, che risponde con un flusso tar: solo file regolari, sotto l'albero, nessun link. Chi riceve lo dà a
    `tree-write`, che valida ogni nome e dà i file al proprietario del **suo** progetto. Un file a metà si scarta e si
    rifà.
- **Integrità.** Ogni file ha il suo SHA-256 nel manifesto, con una cache per (percorso, dimensione, `mtime_ns`, inode)
  in `hashes.json` della sorgente. Il filestore di Odoo si verifica dal suo nome, che è lo SHA-1 del contenuto: niente
  cache e niente hash da calcolare da noi.
- **Ripresa.** Lo stato (giro, lotti finiti) si scrive dopo ogni lotto. Un job ucciso riparte da lì; una consegna
  rifatta con un codice nuovo, dalla stessa macchina, riparte da quello che ha.

**Il database.**

| Caso | Come | Dove sta il codice di oggi |
|---|---|---|
| Dedicato, stesso major e stessa architettura, e la porta raggiungibile | chi riceve crea l'istanza senza avviarla; `ArrivePlace` copia (`pg_basebackup` TLS `verify-full`) nella **cartella dati della nuova istanza** e la segue in `.replica/arrive`. La sorgente la fa entrare con il `allow` di oggi (ruolo di replica, slot, tetto, una riga `hostssl` per un /32, il firewall). Al passaggio: la sorgente si ferma pulita e dice il suo ultimo record, chi riceve lo aspetta (≤ 60 s), promuove, toglie `.replica/arrive`, avvia il servizio `db` dell'istanza | `Dedicated.allow`, `return_stop` (generalizzato a una sorgente dedicata: `machine-db-replica-stop`), `DedicatedBack` / `back_*` (generalizzati: `machine-db-arrive`) |
| Cluster della macchina, major o architettura diversi, o porta non raggiungibile | al passaggio: `pg_dump -Fc` in `backups/<server>/production/handover-<id>.dump`, trasferito come file (con ripresa), `pg_restore -j` nel posto di chi riceve | il backup e il restore di oggi, AD-007 |
| Database di un'altra macchina (AD-003) | non si copia: chi riceve deve essere fatto entrare da chi ha il database. Prima di Passa lo si prova | in A: §A11; in B, nei primi passi è rifiutato con il motivo |

In A l'indirizzo che entra è quello da cui il cliente ha fatto `allow`, letto da noi e mai dichiarato da lui.
In B è l'host del collegamento (o il suo server SSH) risolto in un solo IPv4, come fa `_primary_address`.

**Contratto d'invarianti** (quello che si importa riusando replica e filestore):

| Invariante | Vale? |
|---|---|
| La replica fisica chiede lo stesso major e la stessa architettura | **Non sempre**: c'è una macchina ARM (ber) e il cliente può avere un'altra architettura o PostgreSQL. Controlli confronta major e `uname -m`; se diversi, dump |
| Una sola replica per PostgreSQL (`allow`: «has a read replica already») | **No**, se il database dedicato ha già la replica di UX-011 §D. In A quella si cancella comunque (§A9): va tolta prima di Ricevi, o il database passa per dump. In B: rifiutato con il motivo nei primi passi |
| Un file del filestore non si riscrive mai (il nome è il suo SHA-1) | **Sì**, nel core di Odoo; in più la dimensione si confronta |
| L'uid dei file è lo stesso sulle due macchine | **No** in generale: `tree-write` dà il proprietario del progetto di chi riceve, mai quello della sorgente |
| `DedicatedBack` presume un database «sostituito» da una promozione | **No** per un'istanza nuova: `ArrivePlace` toglie quel presupposto e chiede un'istanza creata e mai avviata, con la cartella dati vuota |

**Conseguenze (vincoli).**
1. Helper nuovi:
   - `tree-list <ws> <storage> <istanza> <ruolo>`: NDJSON su stdout;
   - `tree-read …`: percorsi su stdin, tar su stdout;
   - `tree-write …`: tar su stdin; rifiutato se l'Odoo di quel progetto gira;
   - `tree-prune …`: nomi da tenere su stdin; solo all'ultimo giro;
   - `machine-db-arrive <op> <chiave>`, con `op` prepare, copy, start, state, wait, promote, cancel; segreti su stdin;
   - `machine-db-replica-stop <chiave>`.
2. `allow` accetta come richiedente un collegamento **o** una consegna (`handover:<id>`): oggi `LINK_NAME` rifiuta
   tutto il resto.
3. Nessun manifesto intero in memoria: si scrive su file e si confronta in ordine (un filestore da 500.000 file sono
   circa 25 MB di righe).
4. Prima di ogni giro chi riceve controlla lo spazio con il suo `space`. Da noi lo spazio in più serve solo ai dump
   del cluster, al passaggio.

**Tracciabilità.** UX-014 A6, A11 («la copia si interrompe»), B4 passo 3, B5; AD-004 (replica), AD-007 (dump).

## AD-011 — Il passaggio: due fasi, coordina chi ha i dati adesso, tutto o niente

**Contesto.** UX-014 A6 e B5:
- le istanze partono senza cron e senza traffico;
- i nomi passano solo quando tutte rispondono 200;
- se una sola fallisce, tutte tornano a girare da noi senza perdere niente;
- il passaggio lo preme il cliente (D-A4), lo spostamento l'integratore.

`zerodowntime.py` misura il fermo chiedendo il nome ogni 100 ms. `cron_guard.py` e `composehelper` (righe 394-408)
spengono e riaccendono `ir_cron` per id.

**Opzioni considerate.**
- A. **Due fasi**: la sorgente coordina e decide il commit; chi riceve esegue, prova e chiede il commit.
- B. Coordina chi riceve: se il cliente sparisce a metà, da noi le istanze restano ferme senza nessuno che decida.
- C. Senza coordinamento (chi riceve parte, poi avvisa): due produzioni che scrivono (smell di UX-014).

**Decisione.** A.

**Motivazione.** Chi ha i dati adesso è l'unico che può dire «da ora servo io» oppure «da ora no». Una scadenza sul
suo lato chiude ogni caso in cui l'altro sparisce.

**I passi** (A; in B la casa fa sia la sorgente sia chi guida, e l'host risponde alle stesse domande attraverso il
collegamento).
1. **Chi riceve, prima**: il fornitore risponde, il codice vale, `probe` da noi trova le sue porte 80 e 443, il
   database è indietro di ≤ 60 s, l'ultimo giro dei file ha ≤ 10 minuti. Altrimenti Passa è spento con il motivo
   (§A5).
2. **`switch/begin`** (con uno `switch_id` di chi riceve, che rende idempotente ogni passo dopo). Noi, scrivendo lo
   stato **prima** di agire:
   - pagina di manutenzione su ogni ambiente di W;
   - Odoo fermo;
   - dedicato: `machine-db-replica-stop`, poi l'ultimo record;
   - cluster: i dump, 2 alla volta;
   - il manifesto dell'ultimo giro;
   - le pianificazioni di W sospese (non cancellate);
   - lo stato `frozen`, con `deadline = adesso + max(10 min, 3 × restore stimato)`.
3. **Chi riceve**:
   - l'ultimo giro e i dump;
   - aspetta il record e promuove, oppure fa il restore;
   - importa la configurazione (AD-009), con la mappa dei nomi;
   - avvia le istanze con i cron spenti (`UPDATE ir_cron SET active = false WHERE active RETURNING id`, gli id scritti
     nello stato);
   - controlla `/web/login` = 200 su `127.0.0.1:<porta>` di ogni istanza, al massimo 300 s ciascuna.

   Fino al commit il suo nginx serve per quei nomi solo le verifiche ACME e la pagina di manutenzione (un modo `hold`
   del sito, AD-012): nessun utente arriva a un Odoo non confermato.
4. **Un fallimento** → `switch/abort`. Noi: database e Odoo ripartono, la manutenzione si toglie al primo 200, le
   pianificazioni riprendono, lo stato torna `ready`. Chi riceve: ferma le sue istanze, cancella i database
   ripristinati, toglie gli utenti creati da questo passaggio e segna il dedicato «da copiare di nuovo» (dopo una
   promozione non segue più).
5. **Tutte 200** → `switch/commit`, con i nuovi nomi dei nomi del fornitore. Noi:
   - l'inoltro dei nomi del cliente (AD-012);
   - i 301 dei nomi del fornitore;
   - i token di W revocati;
   - lo stato `committed`, con `delete_after = adesso + 7 giorni`.

   Alla risposta, chi riceve apre i suoi siti (toglie `hold`), riaccende i cron per id e chiede i nomi ogni 100 ms. La
   riga finale del job è il fermo misurato, dalla manutenzione al primo 200 attraverso il nome, come `zerodowntime`.
   In B va anche nel deploy dell'istanza.
6. **Scadenza** senza commit né abort: noi annulliamo da soli (job «passaggio scaduto»). Un commit che arriva dopo
   riceve 409, e chi riceve annulla.

**Idempotenza e ripresa.**
- Ogni passo ha una chiave (`id`, `round`, `switch_id`) e si scrive nello stato prima di farlo.
- Un commit ripetuto con lo stesso `switch_id` risponde come la prima volta: la risposta persa non lascia il cliente
  al buio.
- All'avvio di OmniaCLI (come la pulizia dell'avvio di AD-006), su una macchina in `frozen` riparte il timer della
  scadenza. Su chi riceve, un passaggio a metà diventa «interrotto», con Riprova: prima chiede `GET …/switch` e poi o
  continua o torna indietro.
- Durante il passaggio tutti i server di W sono occupati (`server_busy`): nessun altro job ci entra.

**Contratto d'invarianti.**

| Invariante | Vale? |
|---|---|
| Odoo riprende un cron riacceso senza riavvio | **Da provare**: il thread dei cron rilegge `ir_cron` a ogni giro. Se non basta, un riavvio a nomi già passati costa un breve 503 (§9 passo 4) |
| Un Odoo avviato non scrive prima del traffico | **No**: i cron scrivono (posta, sincronizzazioni). Per questo partono spenti; il controllo `/web/login` non scrive dati del cliente |
| La sorgente può ripartire dopo un abort | **Sì**: si è fermata pulita prima che chi riceve promuovesse, e nessuno ha scritto dopo |

**Tracciabilità.** UX-014 A6, A7, A10 (prima di Passa), A11, B4 (Completa), B5; criteri 7 e 13.

## AD-012 — I nomi: Let's Encrypt prima del passaggio, poi la macchina di prima inoltra finché il DNS non punta

**Contesto.** Il sito di un workspace è `/etc/nginx/conf.d/omniacli-<ws>.conf` (helper `web`). La porta 80 serve le
verifiche ACME da `/var/lib/omniacli-acme`, Let's Encrypt è HTTP-01 (lineage `omniacli-<ws>`) e il certificato del
cliente sta in `/etc/omniacli/certs/<ws>/`. `relay.py` scrive già un sito che passa ogni richiesta, il websocket
compreso, a un'altra destinazione con `X-Real-IP`. La «prova di un nome» (un token nella cartella delle verifiche,
chiesto su `http://<nome>/`) dice dove punta il DNS. D-A9 e D-B3 chiedono che la macchina di prima inoltri finché il
DNS non punta.

**Opzioni considerate.**
- A. **Inoltro a livello HTTP** dalla macchina di prima: termina il TLS con il certificato che ha già e ripassa in
  HTTPS verificato alla nuova.
- B. Inoltro a livello TCP (`stream` con `ssl_preread`): non decifra, ma la 443 della macchina diventa un `stream`
  davanti a tutti i siti. È un cambiamento dell'nginx di ogni macchina, per tutti i clienti.
- C. Nessun inoltro: un TTL di 3600 s vuol dire fino a un'ora di errore.

**Decisione.** A. Più una cosa che lo rende possibile senza errori: **prima del passaggio**, la macchina di prima
inoltra **solo** le verifiche ACME dei nomi del cliente. Il certificato nasce sulla nuova macchina mentre la vecchia
serve ancora.

**I tre modi** del nuovo helper `forward <ws> acme|full|off`, con la specifica su stdin e un record in
`/etc/omniacli/forward/<ws>.json`:
- **`acme`** (da `ready`): la porta 80 dei nomi cerca il token da noi e, se non c'è, lo chiede alla nuova macchina
  (`try_files … @forward`); i rinnovi nostri non cambiano. Il resto non cambia.
- **`full`** (al commit): un sito a sé, `omniacli-forward-<ws>.conf`, prende quei nomi, che il sito del workspace non
  ha più:
  - la 80 come in `acme`, il resto alla 80 della nuova macchina;
  - la 443 con il nostro certificato di oggi, `proxy_pass https://<nuova>`, `proxy_ssl_server_name on`,
    `proxy_ssl_name $host`, `proxy_ssl_verify on` (CA di sistema), websocket, `X-Forwarded-For`.

  Per i nomi del **fornitore**: un `301` al nome nuovo per 7 giorni.
- **`off`**: il sito e il record se ne vanno; `nginx -t`, altrimenti torna il sito di prima, come fa `web`.

**Fine dell'inoltro**: ogni 10 minuti la macchina di prima chiede dove punta ogni nome (lo stesso controllo dei nomi).
Quando tutti puntano altrove, `forward off`. Altrimenti si spegne con la cancellazione della copia (A: 7 giorni; B:
con la copia ferma). B con «Lascia i nomi su lab1» (l'alternativa di D-B3): `full` per sempre, il certificato resta su
lab1, e lo dice la riga della placement.

**Contratto d'invarianti.**

| Invariante | Vale? |
|---|---|
| HTTP-01 vuole che la richiesta di Let's Encrypt arrivi a chi ha il token | **Sì** con `acme`: arriva a noi (il DNS punta qui) e la passiamo |
| La 443 della nuova macchina ha un certificato valido prima del commit | **Sì** con `acme` acceso da `ready`. Se la nuova macchina non ha ancora un certificato, il commit aspetta (Passa spento con il motivo): niente 502 (criterio 7) |
| Un workspace pubblicato via relay porta il relay con sé | **No** (§A8): Passa è spento senza un indirizzo pubblico |

**Conseguenze (vincoli).**
1. Il riepilogo di Prepara lo dice in chiaro: per al massimo 7 giorni il traffico di chi ha il DNS vecchio passa da
   noi, decifrato e ricifrato (GDPR). È il prezzo dell'opzione A; B lo toglierebbe, a un costo che non vale una
   finestra di 7 giorni.
2. `web` impara il modo `hold` (per quei nomi solo le verifiche ACME e la pagina di manutenzione). Lo usa chi riceve
   fino al commit, e in B l'host, per i nomi dell'istanza, fino al Completa.
3. Il nostro lineage `omniacli-<ws>` vive finché vive l'inoltro e si cancella con la copia (`certbot delete`).

**Tracciabilità.** UX-014 A8, D-A9, B6, D-B3; criterio 7.

## AD-013 — Dopo il passaggio: la copia ferma per 7 giorni, la cancellazione, Riaccendi, e il registro sulle due macchine

**Contesto.** D-A3: copia ferma e backup per 7 giorni, poi tutto cancellato. Il registro degli accessi resta, per la
sua conservazione. `remove` (con `fstrim`), `drop` (il disco del workspace), `web` e `certbot delete` esistono già. Il
giro di ogni ora e la pulizia all'avvio esistono già per il ripristino a un minuto (AD-006). I container hanno
`restart: unless-stopped`: fermi restano fermi anche dopo un riavvio.

**Decisione.**
- **Copia ferma**: W in stato `consegnato`. Le istanze sono ferme; monitor, pianificazioni, CI, aggiornamento
  automatico e backup automatici la saltano (un controllo solo, `frozen(ws)`, letto da ognuno). Gli utenti di W che
  entrano dal nostro portale leggono il testo di §A12 e nessuna istanza. I token di W sono già revocati (AD-011).
- **Cancellazione**: un job `handover-delete`, dal giro di ogni ora quando passa `delete_after`, oppure subito con
  Elimina adesso la copia. Mette insieme i helper che esistono, nessuno nuovo:
  - `remove` di ogni istanza;
  - la cancellazione delle staging di W su altre macchine, attraverso i loro collegamenti (Elimina di oggi);
  - le cartelle dei backup dei server di W;
  - `forward off`, `web` senza nomi, `drop` del disco;
  - `certbot delete --cert-name omniacli-<ws>`, `/etc/omniacli/certs/<ws>/`;
  - le voci di W in `workspaces.yaml`, `schedules.yaml`, `destinations.yaml`, `credentials.yaml`, `deploy-hooks.json`,
    `deploys/`;
  - gli utenti che sono solo di W, con `second-factor.json`, `passkeys.json` e `sessions.json`;
  - `handover/out/<ws>/`.

  Un collegamento che non risponde per una staging: il job finisce «parziale», ci riprova ogni ora, la banner lo dice,
  e `delete_after` non si sposta. In fondo, una voce del registro con cosa è stato cancellato (quanti file, quanti GB).
- **Riaccendi la copia** (nome scritto, prima della cancellazione): `forward off`, i siti di nuovo, le istanze
  avviate, le pianificazioni riprese. I token restano revocati, e i nomi tornano solo se il DNS punta qui. Il cliente
  non viene avvisato (non c'è un canale), e il testo lo dice.
- **Da lui, dopo**: la chiave del claim, `secrets.sealed` e `parts/` sono già cancellati. Lo stato (piccolo) resta 7
  giorni per «Controlla i nomi».

**Il registro, sulle due macchine** (un job e una voce per ogni passo; `event` = `handover` oppure `move`):

| Passo | Da noi (fornitore) | Dal cliente |
|---|---|---|
| Prepara, Rifai il codice, Annulla | chi, cosa è stato scelto, la scadenza | — |
| claim | IP, impronta della chiave, ora | «codice del fornitore usato», la scadenza |
| ogni giro di copia | byte e file presi, dall'indirizzo X | byte e file ricevuti, «dal fornitore» |
| allow e drop del database | indirizzo, porta (della macchina) | — |
| begin, commit, abort, scaduto | `switch_id`, fermo, esito | chi ha premuto Passa, fermo, esito |
| import | — | utenti, ruoli e segreti importati (i nomi, mai i segreti) |
| forward acme, full, off | i nomi | — |
| cancellazione, Riaccendi | chi, cosa, quanto | — |

**Tracciabilità.** UX-014 A2.4-5, A7, A9, A10, A12, D-A3; criteri 8, 9, 10.

---

## AD-014 — Un'istanza su un'altra macchina, operata dalla casa del suo workspace (D-B2)

**Contesto.**
- **Collegamenti.** Il pannello serve le pagine di un'altra macchina sotto `/links/<nome>/` (`link_proxy`) con
  `X-OmniaCLI-Prefix`, `-User`, `-Role-Cap` (abbassa soltanto) e `-Menu`. Dall'altra parte il collegamento diventa un
  `Principal(kind="link")` **senza ambito**: per costruzione dietro un collegamento c'è l'integratore. Gli utenti di
  un workspace non usano collegamenti (`WORKSPACE_ROUTES` non li contiene).
- **Cosa decide cosa vede il cliente.** `in_workspaces` e `capabilities` lo decidono sulla macchina che risponde;
  `customerview` redige dove la pagina viene scritta.
- **REST.** L'API REST prende solo token (`api_token`).
- **Pianificazioni, monitor, CI.** Le pianificazioni girano sulla macchina dell'istanza (`schedules.yaml`), il monitor
  è locale, il webhook della CI arriva sul nome dell'istanza o sull'indirizzo del pannello (`deploy-hooks.json`).
- **Staging su un'altra macchina.** La staging su un'altra macchina (`staging_elsewhere`, `/api/stagings/receive`)
  nasce in un workspace omonimo fatto là (`quick_workspace`): la elencano solo gli admin della macchina, e il cliente
  non la opera.

**Opzioni considerate.**
- A. **Ambito delegato attraverso il collegamento**: la casa inoltra la richiesta del cliente all'host, e l'host la
  serve come a un cliente (i suoi `in_workspaces`, `capabilities`, `customerview`).
- B. Un plugin «istanza remota» sulla casa, che opera l'host con le sue API: ogni capacità rifatta a distanza (Moduli,
  Deploy, Posta, scheda Database, log, Entra come, shell), con due rendering da tenere allineati. È la dimensione di
  `plugin_api.py` più le route.
- C. Utenti copiati sull'host e un secondo portale: è contro D-B2 (lo stesso portale) e lascia due copie
  dell'identità.

**Decisione.** A.

**Motivazione.** È il meccanismo che il codice ha già (`link_proxy`, il ruolo che si può solo abbassare): gli si
aggiunge l'unica cosa che manca, l'ambito del cliente. Un'intestazione che restringe soltanto, mai allarga.

**Il meccanismo.**
1. **Record.**
   - Sulla casa: `placements.json`, `{server: {link, state: preparing|ready|placed|returning, since, copy_until,
     names: forward|moved|permanent, revision}}`. Il server resta in W (`workspaces.yaml`: membri, `member_servers` e
     ruoli non cambiano). La sua copia ferma resta sul disco, ma non è più un server che riceve richieste.
   - Sull'host: `hosted-workspaces.json`, `{ws: {home: <nome del collegamento in entrata della casa in auth.links>,
     provider_label, members, revision}}`. Il W dell'host non ha membri suoi.
2. **Instradamento sulla casa.** Le richieste su un server spostato vanno all'host, sotto il prefisso
   `/placed/<server>` (307 dai percorsi di prima): `/servers/{server}/…`, `/api/servers/{server}/…`,
   `/backups/{server}/…`, `/x/…`, `/api/v1/servers/{server}/…`, i websocket (`proxy_websocket`). Il prefisso non dice
   la macchina: i job dell'host stanno sotto lo stesso prefisso, quindi i loro id non si confondono con quelli della
   casa.
3. **L'intestazione `X-OmniaCLI-Scope`** (JSON in base64url): `{ws, person, kind: user|token, role, grant:
   {capabilities, servers}|null, members}`. L'host la accetta solo se:
   - il token del collegamento è la `home` di `ws` in `hosted-workspaces.json`;
   - quel collegamento ha il ruolo admin.

   Allora costruisce `Principal(name=person, scoped=True, workspaces=((ws, min(role, ruolo del collegamento)),),
   grants=…)`, con `on_behalf`. Da un altro collegamento: 403 e una voce nel registro. Senza l'intestazione, tutto come
   oggi (l'integratore).
4. **Due controlli.** La casa controlla per prima, con il suo `in_workspaces` sulla stessa route (le route sono le
   stesse); l'host controlla di nuovo. Un difetto della casa non allarga quello che l'host permette, e viceversa.
5. **Cosa legge il cliente.** L'host scrive la pagina con `customerview`: `provider` è il `provider_label` della casa,
   `members` arrivano dall'intestazione. Il job registra la persona (`manual: anna`), il registro dell'host il
   collegamento con `on_behalf` e `workspace`.
6. **REST e MCP.** Un token di W sulla casa che chiama un server spostato: la casa inoltra a `/api/v1` dell'host con
   `kind: token`; `api_token` dell'host accetta un collegamento che porta l'ambito di un token. Gli strumenti MCP della
   casa, per un server spostato, chiamano la stessa funzione di inoltro.
7. **Le liste.** Dashboard, Istanze, Backup, Workspace, job, mappa e campanella aggiungono le righe dell'host, chieste
   con lo stesso ambito (5 s, come le staging di oggi). Una riga che non risponde dice «la macchina del fornitore non
   risponde» al cliente, «lab2 non risponde da …» all'admin.

**Cosa sta dove.**

| Sulla casa (lab1) | Sull'istanza (lab2) | Copiato dalla casa all'host (`POST /api/placements/{ws}/sync`, idempotente per `revision`) |
|---|---|---|
| il workspace, utenti, ruoli, persone, token, OAuth, SSO, il portale, il registro delle richieste delle persone; la configurazione dei nomi; destinazioni e credenziali git (l'originale) | progetto, database, filestore, backup locali, opzioni di Odoo, CI con il segreto del webhook, avvisi, pianificazioni dell'istanza, storico dei deploy, monitor, i suoi job | dominio, sottodomini, indirizzi e rinvii delle istanze spostate, certificato del cliente, le destinazioni e le credenziali che quelle istanze usano, `provider_label`, i nomi dei membri |

**Come seguono.**
- **Backup**: li fanno l'host e le sue pianificazioni, sulle sue destinazioni copiate dalla casa. Si scaricano
  attraverso la casa (la banda passa da lab1, e il README lo dice).
- **Pianificazioni**: quelle che nominano il server passano all'host con lo spostamento; sulla casa restano sospese,
  per il ritorno.
- **Webhook della CI**: un indirizzo sul nome dell'istanza arriva all'host (dopo il DNS o con l'inoltro di AD-012); un
  indirizzo sul pannello della casa la casa lo inoltra all'host com'è (corpo e firma intatti).
- **Nomi e certificati**: AD-012; un nome cambiato sulla casa si copia all'host e l'host lo pubblica (`web`).
- **Monitor e campanella**: controlla l'host; la mappa della casa lo legge dal collegamento (il livello che esiste
  già) e la campanella del cliente filtra per ambito.

**Quando qualcosa non risponde.**
- **L'host è giù**: l'istanza è giù (gira lì); la casa mostra le righe che non rispondono. Nessun comando resta in
  coda: come per i collegamenti, un'azione rifiutata si dice.
- **Il collegamento è giù, le due macchine su**: Odoo serve i suoi utenti, backup e pianificazioni girano sull'host,
  il cliente non opera dal portale e niente si perde.
- **La casa è giù**: Odoo sull'host serve, il portale no. È il prezzo di D-B2: la casa è un punto unico per operare,
  non per servire. Finché c'è l'inoltro di AD-012, i nomi che puntano ancora alla casa si fermano: per questo l'avviso
  sul TTL.

**Il verso delle chiamate.** Chiama la casa, l'host risponde; **l'host non ha mai un token verso la casa**. È diverso
dalla regola di AD-004 («chiede chi ospita la copia») per un motivo preciso: qui chi opera è la casa, e un host con un
token verso la casa potrebbe riscrivere gli utenti del cliente.

**Contratto d'invarianti** (riusare il collegamento e il suo proxy):

| Invariante | Vale? |
|---|---|
| Dietro un collegamento c'è l'integratore (`Principal` di un collegamento senza ambito) | **No** per il cliente: per questo l'intestazione d'ambito, che restringe soltanto. Senza, resta vero |
| Il W dell'host ha i suoi membri (`customerview.members`, i ruoli nel workspace) | **No**: i membri stanno sulla casa. Arrivano con l'intestazione e con la copia |
| L'id di un job è unico sulla macchina che lo mostra | **No** fra due macchine: per questo il prefisso `/placed/<server>` |
| Un nome di server è unico sulla macchina | **Sì**: la casa tiene il nome, l'host lo ha in W con lo stesso nome, e la copia ferma della casa non riceve richieste |
| L'API REST la usa solo un token | **No** per un token della casa che passa dal collegamento: `api_token` accetta un collegamento che porta l'ambito di un token, e nient'altro |

**Conseguenze (vincoli).**
1. Le route nuove sull'host, solo per un collegamento admin che sia `home`:
   - `GET /api/placements/host`, come `/api/stagings/host`: template, spazio, versione, architettura, major di
     PostgreSQL;
   - `POST /api/placements/{ws}/{istanza}/arrive|start|crons|abort|release`;
   - `PUT  /api/placements/{ws}/{istanza}/trees/{tree}`;
   - `POST /api/placements/{ws}/{istanza}/db/{op}`;
   - `POST /api/placements/{ws}/sync`.
2. Un test passa ogni route di `WORKSPACE_ROUTES` per un server spostato, con ogni ruolo e con un ruolo
   personalizzato (come `tests/test_portal_access.py`), e confronta le risposte con quelle dello stesso server rimasto
   in casa.
3. Fino al passo 5 di §9, lo spostamento è rifiutato per un workspace con membri (l'alternativa 1 di D-B2 come
   cancello temporaneo).

**Tracciabilità.** UX-014 B1, B2.1, B6 (cliente), B7, D-B2; criterio 15.

## AD-015 — Lo spostamento: Prepara, Completa, Torna, Elimina la copia

**Decisione.** Lo spostamento è AD-010, AD-011 e AD-012 nel verso casa → host, con il driver **push** e la casa come
sorgente e coordinatore.
- **Prepara** (`POST /api/servers/{server}/move` con `link`; job: Controlli · Su lab2 · Copia · Al passo).
  - Controlli: `GET /api/placements/host`. È rifiutato con il motivo per un collegamento in HTTP non in tunnel, un
    OmniaCLI più vecchio, lo spazio che manca, un database di un'altra macchina, un dedicato che ha già una replica,
    un job in corso.
  - L'host prepara: crea W se manca (`quick_workspace`, con i limiti di W) e lo segna `home`; crea l'istanza dallo
    stesso template, senza avviarla; pubblica il sito in `hold`.
  - La copia: `treesync` push di repository, moduli caricati, filestore, deploy e **backup locali**. Per un dedicato,
    `ArrivePlace` sull'host e `allow` sulla casa.
  - La casa accende `forward acme`, e lo stato diventa `ready`.
- **Completa** (`…/move/complete`, con il nome dell'istanza scritto): AD-011. Si può solo con le due macchine che
  rispondono e il database indietro di ≤ 60 s. Il fermo misurato va nella riga del job e nel deploy. La copia ferma
  resta sulla casa fino a `copy_until` = +7 giorni (D-B4).
- **Annulla** (prima di Completa): l'host cancella l'arrivo (`machine-db-arrive cancel`, `remove`), la casa fa `drop`
  della replica e `forward off`.
- **Torna su lab1** (`…/move/back`, poi `…/move/back/complete`): lo stesso al contrario, con il driver **pull** dalla
  casa.
  - I file: solo la differenza con la copia ferma.
  - Il database dedicato: la casa chiede all'host di farla entrare (`allow` sul dedicato dell'host), mette da parte i
    dati della copia ferma come fa oggi il ritorno, li sostituisce con una **copia intera** (`pg_basebackup`) e la
    segue.
- **Elimina la copia** (prima della scadenza): `remove` della copia ferma, le pianificazioni sospese cancellate.
- **Staging (D-B5)**: una staging sulla casa la cui produzione è stata spostata **Ricostruisce** con un backup preso
  attraverso il collegamento (`linkcopy.fetch_backup`, quello di Copia dal collegamento). Il rifiuto di oggi («la
  produzione di … è su …») cade per una produzione spostata.

**Conseguenze (vincoli).**
1. UX-014 B6 dice che Torna su lab1 «copia solo le modifiche». Vale per i file; **non per il database dedicato**, che
   torna intero (1,8 GB ≈ 30 s a 60 MB/s, stima). `pg_rewind` lo eviterebbe, ma chiede `wal_log_hints` o i checksum,
   e non ho visto se i cluster dei template li hanno.
2. B2.1 («stessi backup») significa che i backup locali viaggiano in Prepara, a istanza in funzione: tempo e spazio
   sull'host vanno nel piano, accanto ai 3,3 GB. È una decisione per Carlo (§10).

**Tracciabilità.** UX-014 B2-B6, D-B3, D-B4, D-B5; criteri 11-14.

---

## 8. Vincoli non funzionali (per il dev-lead)

1. **Sicurezza.**
   - Codice: 256 bit, 72 h, un claim, conservato come hash, con il pin del certificato.
   - Segreti sigillati alla chiave del claim, mai su disco in chiaro da noi.
   - Una porta del database per un solo /32, chiusa al commit, all'abort, ad Annulla e alla scadenza.
   - L'intestazione d'ambito restringe soltanto.
   - Nessun token dell'host verso la casa; nessun segreto negli argomenti di un helper o in un log.
2. **Minacce e difese.**
   - *Codice rubato prima del claim*: 72 h; claim unico (il cliente vero vede «già usato» e lo dice); avviso immediato
     del claim con IP e impronta nella banner e nella campanella; Annulla interrompe la copia. Il rischio che resta:
     chi ruba il codice e lo usa per primo copia i dati finché il fornitore non annulla. È detto nella frase del
     codice («mandalo come una password»).
   - *Uomo in mezzo*: TLS verificato più il pin `p`; la replica in `verify-full` con la CA del database.
   - *Un cliente che non è chi dice*: il consenso è il nome scritto da PA1 in Prepara; l'identità del destinatario la
     verifica PA1 fuori banda, e le impronte sulle due parti lo permettono.
   - *Segreti in viaggio*: TLS più il sigillo.
   - *Segreti fermi*: da noi restano nei file di oggi (600 o di root); da lui sono sigillati fino all'import, poi chiave
     e blocco si cancellano.
3. **Disponibilità.**
   - Fermo misurato e scritto.
   - Tutto o niente.
   - Scadenza con annullamento automatico.
   - Chi riceve non serve prima del commit (`hold`).
4. **Dati del cliente.**
   - Da noi tutto è cancellato a `delete_after`, con la voce nel registro.
   - I dump del passaggio vanno nella cartella dei backup e si cancellano con la copia.
   - Niente dati del cliente in `/var/lib/omniacli/handover` oltre a stato, hash e chiavi.
5. **Risorse.**
   - 4 flussi per consegna, lotti tar di ≤ 256 MiB, manifesti su file e mai interi in memoria.
   - Lo spazio si controlla prima di ogni giro, dalle due parti.
6. **Redazione.** Dalla parte del cliente (A) e per il cliente sulla casa (B) nessun nome di macchina, indirizzo o
   cartella del fornitore: nel DOM, nel JSON, nei job e nel pacchetto (criteri 6 e 15).

**I file nuovi** (in `/var/lib/omniacli`, dell'utente di OmniaCLI, 600 in cartelle 700):
- `handover/out/<ws>/`: `state.json` (hash del codice e della sessione, scadenza, scelta, claim, fase, `switch_id`,
  `deadline`, `delete_after`) e `hashes.json`;
- `handover/in/<id>/`: `state.json`, `claim.key`, `secrets.sealed`, `parts/`;
- `move/<server>/state.json`;
- `placements.json` e `hosted-workspaces.json` (con i loro `.tmp`).

Di root: `/etc/nginx/conf.d/omniacli-forward-<ws>.conf` e `/etc/omniacli/forward/<ws>.json`. Il **purge**
(`packaging/debian/files/postrm`) aggiunge:
- `rm -rf /var/lib/omniacli/handover /var/lib/omniacli/move`;
- i due JSON con i `.tmp`;
- `rm -f /etc/nginx/conf.d/omniacli-forward-*.conf`;
- `rm -rf /etc/omniacli/forward`.

**I helper nuovi** (`workspace-helper`):
- `tree-list`, `tree-read`, `tree-write`, `tree-prune`;
- `machine-db-arrive`, `machine-db-replica-stop`;
- `forward`, `owncert-read`;
- `maintenance` (dal ramo `pending-stop-during-update`).

Il resto è riuso: `create`, `remove`, `drop`, `limits`, `web` (con `hold`), `owncert`, `template-install`,
`enterprise-sync`, `machine-db-replica-allow/drop/state`; da `compose-helper`, `stop-service` ed `exec` per i cron.

**I moduli nuovi**:
- `omniacli/handover.py`: codice, stato, manifesto, sigillo (le primitive di `pullcrypto`), estratto del registro;
- `omniacli/treesync.py`;
- `omniacli/placement.py`: record, intestazione d'ambito, copia dalla casa all'host, instradamento.

Le route vanno in `app.py`, come per le staging.

## 9. Ordine di costruzione (ogni passo si rilascia e si prova su lab3/lab4; ogni passo si annulla)

0. **Unire `pending-stop-during-update`** (pagina di manutenzione) in `carlo_dev_2026`. *Prova*: il test di `dcf5cab`
   su lab4, 503 e nessun 502.
1. **`treesync` e i helper `tree-*`**, senza interfaccia: una route nascosta, solo admin, copia il filestore di
   un'istanza da lab3 a lab4 attraverso il collegamento.
   - *Prova*: un job ucciso a metà riprende dal lotto; un file alterato viene rifiutato (lo SHA-1 del nome); `../` e
     un link simbolico vengono rifiutati; i proprietari sono quelli di lab4.
   - *Si misura*: i MB/s.
   - *Si annulla*: togliendo la cartella di prova.
2. **`ArrivePlace`** (`machine-db-arrive`, `machine-db-replica-stop`), da un dedicato di lab3 a un'istanza nuova su
   lab4.
   - *Prova*: la copia, il passo in secondi, stop, attesa, promozione; il dato scritto un attimo prima dello stop c'è.
   - *Si misura*: il tempo senza database.
3. **`forward acme|full|off` e `hold`**.
   - *Prova* su lab3: i nomi di un workspace passati a lab4 con `curl --resolve`; la 443 verificata con una CA di
     prova; nessun 502 durante il cambio; `off` e `nginx -t` che rimette il sito di prima.
   - HTTP-01 vero chiede un nome pubblico: su lab3/lab4 (indirizzi privati) si prova solo il passaggio della richiesta.
     Il certificato vero si prova sulla prima macchina pubblica (Non verificato).
4. **Spostamento B per istanze senza membri** (AD-015 Prepara, Completa, Annulla, Elimina la copia; un dedicato e una
   del cluster; staging che ricostruiscono).
   - *Prova*: criteri 11-13; sonda ogni 100 ms, fermo ≤ 90 s, 0 risposte 502, la riga nel deploy; abort provocato
     (un'istanza che non parte), tornata 200 su lab3.
   - *Si misura*: l'avvio di Odoo e la ripresa dei cron senza riavvio.
5. **Ambito delegato** (AD-014): gli utenti del workspace su lab3 operano l'istanza su lab4.
   - *Prova*: criterio 15; il test di accesso per ogni route di `WORKSPACE_ROUTES` (ruoli, ruolo personalizzato,
     token, MCP); 0 «lab3»/«lab4» nella pagina e nei job del cliente; lab4 spenta o collegamento tagliato, le righe
     che lo dicono e Odoo che serve.
   - Toglie il cancello del passo 4.
6. **Torna su lab3** (AD-015).
   - *Prova*: criterio 14, una riga scritta su lab4 presente su lab3 dopo il ritorno, 3 volte su 3; i file portati per
     differenza (i byte nel job).
7. **Consegna A, fino a «pronto»**: Prepara, codice, `offer`, `claim`, Ricevi su **lab4 dietro il NAT simulato**,
   Annulla dalle due parti, Rifai, scadenza.
   - *Prova*: criteri 1-6; 0 regole in entrata nuove sul firewall di lab4; la replica dedicata dalla porta pubblicata
     di lab3 verso l'uscita di lab4; il pin rifiutato con un certificato cambiato.
8. **Passa**: due fasi, scadenza, abort, import di utenti, TOTP, ruoli, pianificazioni, destinazioni, credenziali,
   certificato ed estratto del registro.
   - Per questo passo lab4 va **raggiungibile** sulle 80 e 443 (port forward sul NAT simulato): Passa vuole un
     indirizzo pubblico (§A8).
   - *Prova*: criteri 7, 8, 10; la stessa password e lo stesso codice TOTP su lab4; un commit perso e ripetuto; la
     scadenza superata, lab3 di nuovo su.
9. **Dopo**: la copia ferma, la banner, il giro di ogni ora, Elimina adesso, Riaccendi.
   - *Prova*: criterio 9; dopo la cancellazione, 0 file sotto il disco di Cliente1, 0 backup, 0 container, 0 unit, il
     lineage cancellato, 1 voce nel registro.
10. **REST e MCP** (`prepare_handover`, `receive_workspace`, `switch_workspace`, `move_instance`), README, testi
    italiani, purge.
    - *Prova*: il purge su lab4 non lascia niente di §8.

## 10. Da confermare con Carlo (la raccomandata per prima)

- **Backup locali nello spostamento B.**
  - Raccomandata: viaggiano tutti in Prepara, a istanza in funzione, con la dimensione detta nel piano.
  - Alternativa: solo l'ultimo di ogni ambiente; gli altri si cancellano con la copia ferma, e la storia dei backup
    dell'istanza si perde dopo 7 giorni.
- **Torna su lab1 per un database dedicato**: copia intera (1,8 GB ≈ 30 s a 60 MB/s, stima). Il testo di B6 va
  corretto: «copia solo le modifiche» vale per i file.
- **Il traffico che passa da noi decifrato fino a 7 giorni** (AD-012 A): lo dice il riepilogo. Se non va, l'inoltro TCP
  (AD-012 B) costa un nuovo davanti per la 443 di ogni macchina.
- **Un nome di utente che il cliente ha già**: entra come `<nome>.<ws>`. UX-014 non lo prevede: serve una riga nel
  passo 2 di §A5.

### Decise da Carlo (23/11/2026)

Backup locali nello spostamento B: viaggiano tutti. L'inoltro decifrato fino a 7 giorni (AD-012 A) va bene, il
riepilogo lo dice. Un nome di utente che il cliente ha già entra come `<nome>.<ws>`. «Torna su lab1» per un database
dedicato copia il database intero (accettato con la raccomandata); il testo di UX-014 B6 va corretto.

## Non verificato

- `dcf5cab` (la pagina di manutenzione) non è su `carlo_dev_2026`: UX-014 §0 la dà per esistente.
- Che la porta pubblicata di un dedicato (15432 e seguenti) si raggiunga da fuori sulle nostre macchine vere: su
  inaria il fornitore apre solo 22528 (AD-004 §9). Se no, il dedicato passa per dump, con il fermo più lungo.
- Che dietro un NAT l'indirizzo d'uscita della chiamata HTTPS sia quello della replica.
- Che il filestore sia leggibile solo da root (uid del container): dedotto dal template, non guardato su una macchina.
- Che Odoo 16-20 riprenda un `ir_cron` riacceso senza riavvio: da provare al passo 4.
- `pg_rewind` (`wal_log_hints`, checksum) sui cluster dei template: non letto.
- La conversione del fuso delle pianificazioni (stesso istante o stessa ora, l'ora legale): lasciata al dev-lead su
  §A11.
- Che `staging_receive` porti il codice ai commit della produzione per una staging su un'altra macchina: il README lo
  dice, il codice della ricezione non l'ho letto.
- Come gli strumenti MCP (`mcp_server.py`) scelgono il server: non letto. Il vincolo di AD-014 punto 6 vale com'è.
- Le passkey con il portale nostro e quello del cliente sotto lo stesso dominio registrabile (UX-014, Non verificato).
- Le staging su una terza macchina di una produzione spostata (D-B5): la casa le elenca come oggi; la loro
  Ricostruisci non l'ho progettata.
- Nessuna misura: tutti i tempi di §0 diversi dai 10 s del database sono stime.
