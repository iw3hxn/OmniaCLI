# AD-019: Il monitor di un'istanza: raccolto sulla macchina, dal log di Odoo e da query fisse

Stato: **deciso** (le risposte di Carlo in fondo). Risponde a UX-018 §9 e alle sue tre domande. Codice letto a `ba3da2c` (`carlo_dev_2026`), sorgenti Odoo 16-19 in locale. I numeri stimati lo dicono; la fetta 0 li misura. Valgono le regole di AD-017 e AD-018: la macchina non chiama il centrale, REST e MCP vanno insieme, il registro NIS 2, la redazione `customerview` fatta sulla macchina. Origine: Carlo, 26/11/2026. Nessun `TS-NNN`, `T<n>`/`HT<n>`.

## 0. Com'è oggi

| Cosa | Oggi | Fonte |
|---|---|---|
| Storia di un'istanza | 1 ora in memoria (720 punti da 5 s) | `metrics.HISTORY` |
| Disponibilità | 1 giorno | `monitor.KEEP` |
| Log compose | json-file di Docker, 50 MB × 5 | template `odoo-1x` |
| nginx | i siti senza `access_log`: `combined` in `access.log`, **senza host né tempo** | `workspaceweb.conf_text` |
| Limite di richieste | solo sul login del portale; Odoo 16-19 non dà mai 429 | `workspaceweb` 400-407 |
| CPU del cluster della macchina | **non letta**: il campionatore guarda `system-postgresql.slice`, il cluster è in `omniacli_machine_db.slice` | `metrics.postgres_cpu` |
| Memoria «concessa» | è del workspace (`MemoryMax` della slice), condivisa da produzione e staging | `workspacehelper.slice_text` |

**Il «Non verificato» di UX-018.**
- Le righe `werkzeug` portano il tempo a `log_level` info in 16-19 (`%(perf_info)s`: query, tempo SQL, tempo restante). Dalla 18 hanno un quarto campo, e la 19 aggiunge `#modello.metodo`. Con `log_level = warn` le righe mancano; i template non lo impostano.
- Nessun limite di richieste davanti alle istanze: la serie «Rifiutate» non compare mai.

---

## AD-019: La forma

**Contesto.** UX-018 chiede 14 giorni a 5 minuti per istanza: CPU, memoria, richieste, non riuscite, mediana e 1 su 20, worker occupati, conteggi del log, posta, spazio giornaliero. Le istanze sono di tre forme (compose, systemd, processo) e hanno il database in tre posti (dedicato, cluster, remoto). Per IOPS, CPU per tipo di worker e torte (UX-018 §11) non c'è nessun raccoglitore.

**Opzioni.**
- A. **Tutto nel processo di OmniaCLI**: il campionatore diviso per servizio, il log di Odoo letto in coda, query fisse in sola lettura, file giornalieri append-only.
- B. Prometheus, gli exporter e Grafana.
- C. Le richieste dal log di nginx.

**Decisione.** A (19.1-19.6).

**Motivazione.**
- 少: nessun demone né dipendenza. Si riusano il campionatore, `logtail`, la strada di `pgreport` e i file giornalieri di `consumer.py`.
- 美: una sola fonte per le richieste, il log di Odoo, per ogni forma di istanza.
- 軽: ogni 5 minuti una chiamata per istanza, che legge solo i byte nuovi.

**Contratto d'invarianti.**

| Invariante | Vale? |
|---|---|
| **Ordinamento**: la riga di una richiesta c'è quando comincia | **No**, si scrive alla fine: la richiesta si conta all'ora di fine. I worker occupati si misurano dalle connessioni aperte, non dal log. Gli `id` di `mail_message` non seguono l'ordine dei commit: gli ultimi 2 giorni si ricalcolano |
| **Reversibilità**: un'e-mail inviata resta nel database | **No**: `mail_mail.unlink` cancella anche il messaggio se non è una notifica (letto in 16 e 19). Le inviate si contano dal log, le «non partite» si rileggono |
| **Proprietà**: log e database sono dell'istanza | **Sì**: il monitor tiene solo aggregati, mai righe, IP, query string o indirizzi |
| **Completezza**: quello che si legge è intero | **No**: si legge fino all'ultimo `\n`. Un file ruotato si finisce dal vecchio inode. Un contenitore rifatto si legge prima, al `begin` di `JobManager`. Il resto è un buco dichiarato |
| **Unicità/cardinalità**: un percorso è una chiave limitata | **No** (`/my/orders/123`). Si normalizza (numeri `<n>`, token `<x>`, niente query string) e si tengono le 50 pagine peggiori all'ora. `mail_message` e `ir_attachment` si leggono solo per intervallo di `id` o per aggregato |

### 19.1: Le fonti

| Dato | Fonte | Ogni | Cluster | Remoto |
|---|---|---|---|---|
| CPU, memoria Odoo | cgroup dei contenitori del servizio Odoo, unità o processi | 5 s | sì | sì |
| CPU, memoria, OOM del database | cgroup e `memory.events` del servizio `db` | 5 s | solo P1, la slice intera | no, con la frase |
| Worker occupati (`workers` > 0) | connessioni `ESTABLISHED` sulla porta HTTP, da una sola chiamata `ss -Htn state established` per tutte le porte (non `/proc/net/tcp`: fetta 0); nginx chiude dopo ogni richiesta (HTTP/1.0) | 5 s | sì | sì |
| Risponde | i controlli di `monitor.py`, nel punto | 1 min | sì | sì |
| Richieste, 5xx, tempi, pagine, log | log di Odoo (19.2) | 5 min; 1 min con la scheda aperta | sì | sì |
| Posta, spazio | 19.3, 19.4 | 5 min, 1 giorno | sì | sì |

Mediana e «1 su 20» di un periodo vengono da un istogramma di 12 classi per punto, con bordi a 1 s e 3 s, mai da una media di mediane.

### 19.2: Il log, in coda

- **Venv**: OmniaCLI legge `log_file` con `logtail.read_since`, per offset e inode.
- **Compose**: un'azione del helper legge per offset e inode il json-file del contenitore (`LogPath`). Il driver json-file è obbligatorio (`stacktemplates` 516). Non si usa `docker logs --since`, che rilegge fino a 250 MB a ogni chiamata (fetta 0).
- **Rotazione**: prima si finisce il vecchio inode (`.1`, o la data di `logrotate = True`), poi si legge il nuovo da 0. Un file già compresso è un buco dichiarato.
- **Dopo un fermo**: il log si recupera dal cursore (al più 20 MB per giro); CPU e memoria restano un buco.
- **Righe contate**:
  - le righe d'accesso: logger `werkzeug` in 16-19, `odoo.http.server` in 20 (fetta 0); `/xmlrpc/`, `/jsonrpc`, `/json/2/`;
  - ERROR e WARNING;
  - «virtual memory limit» e «Server memory limit»;
  - «tries left»;
  - le righe della posta.
- **Il parsing è di OmniaCLI**: root restituisce byte, non interpreta testo dei clienti.

### 19.3: La posta

- Inviate: «Mail (mail.mail) … successfully sent» del log.
- Respinte: «Routing mail … bounce» del log.
- Ricevute: `mail_message` con `message_type = 'email'`, per `id`. In 16 si escludono quelle con una riga di `mail_mail`.
- Non partite: `mail_mail` con `state = 'exception'`.
- Staging: le trattenute le conta Mailpit.

Le query sono fisse, in `READ ONLY`, con `statement_timeout` 10 s, e fanno la strada di `pgreport` (helper o psql come Odoo). Costo: 2 giorni di chiave primaria, millisecondi (stima).

### 19.4: Lo spazio

Un punto al giorno alle 05:00, dopo i backup:
- `pg_database_size`;
- la somma di `file_size` di `ir_attachment` per `store_fname` distinto.

**Niente `du`**: 500.000 file sono 30-120 s di IO a disco freddo, contro meno di 1 s della query (stime). Il disco del workspace resta quello di `diskhistory`.

### 19.5: La storia

- **Dove**: `monitor/<server>/<role>/`, nello stato della macchina (600/700), un file per giorno, solo righe aggiunte. Per lo stesso tipo e istante vince l'ultima riga, quindi una ripresa non conta due volte. La cartella è nel purge.
- **Tenuta**: punti e pagine 14 giorni, spazio 400 giorni.
- **Peso**: ≈ 2,2 MB per istanza (stima), con un tetto di 5 MB oltre il quale si tolgono i giorni più vecchi («Misurato dal…»). 20 istanze ≈ 44 MB, al più 100 MB.
- **Peso, ridetto con la fetta 2** (27/11/2026): il tetto resta 5 MB. Al giorno, al più: 288 punti del carico (≈ 130 byte), 2 righe del log per ogni lettura di 5 minuti (≈ 160 byte: una campata si riscrive intera quando la lettura dopo ne porta altre righe), 24 righe di pagine (50 pagine, ≈ 3,4 KB). Fanno ≈ 210 KB al giorno, ≈ 2,9 MB in 14 giorni (calcolato, `test_the_history_stays_under_its_cap`). Il cursore (`cursor.json`, 600) tiene la posizione e le campate delle ultime 2 ore.

### 19.6: Pagina, centrale, API

- **Una route** per istanza e periodo (`1h`, `24h`, `7d`, `14d`), con REST `/api/v1/instances/{server}/{role}/monitor` e uno strumento MCP di sola lettura: stessa funzione, stessa redazione.
- **Il registro**: la route è in `SENSITIVE_READS`. Il monitor non ha azioni nuove.
- **Il centrale** scrive la scheda con il suo `server.html` (AD-017, fetta 3), e il JS legge JSON da `/links/<m>/api/…`. È un'aggiunta in `agent_api` 4: una macchina vecchia dà 404, e la scheda dice «aggiornala».
- **P2**: la macchina toglie la slice del cluster, gli host, i percorsi e i nomi.

### 19.7: Le domande di UX-018

1. **Chi legge il log.** Di una venv, OmniaCLI dal file. Di un'istanza spostata (AD-014), l'host dove gira Odoo, e la casa mostra la scheda da `/placed/<server>`. Un'istanza compose ha sempre il log di Docker; la frase «non scrive un file» vale solo per una venv su stdout o nel journal.
2. **Il costo per 20 istanze.** Una lettura del log ≈ 0,13 s di CPU (misurato, fetta 0: 0,10-0,12 s di avvio del helper, 16 ms per 64 KB da un offset). 20 × 0,13 s ogni 300 s fanno ≈ 0,9% di un core, leggendo solo i byte nuovi.
3. **La storia di un'istanza che si sposta.** La segue, nel delta finale del Completa, nel ritorno e nella consegna (≤ 5 MB).

---

## Misure della fetta 0 (26/11/2026, lab3)

Misurato su lab3 (16 core, Docker 28, Odoo 19.0 `odoo:19.0`), su `acme-zipt-stg-20261122` portata a `workers = 4` per la prova e rimessa a 0 dopo. Sui lab c'è solo Odoo 19: per 16-18 e 20 vale la lettura dei sorgenti (`netsvc.py`; in 20 `odoo/http/server_log.py`).

- **Le righe d'accesso, vere (19)**: `2026-11-26 15:14:15,459 14 INFO zipt_stg_20261122 werkzeug: 10.213.4.1 - - [26/Nov/2026 15:14:15] "POST /web/webclient/version_info HTTP/1.1" 200 - 2 0.002 0.010`. Tre campi di tempo (query, tempo SQL, il resto) su `/web/login`, JSON-RPC, XML-RPC e un 404; il quarto (`cursor_mode`, 18 e 19) compare solo dove una richiesta l'ha impostato, e su queste route mai. La richiesta senza database porta `None` al posto del nome.
- **Odoo 20 cambia logger** (sorgenti di ottobre 2026): la riga è di `odoo.http.server`, non di `werkzeug`, e porta sempre quattro campi (`cursor_mode` vale `-`). Il lettore della fetta 2 accetta tutti e due: **corretto in 19.2**.
- **XML-RPC in 19 scrive un avviso a ogni chiamata** («Mute this logger: --log-handler odoo.addons.rpc.controllers.xmlrpc:ERROR»): nella fetta 2 i WARNING di quel logger non si contano fra gli avvisi, solo fra le chiamate dalle API.
- **Byte di log**: ≈ 216 byte per richiesta nel json-file di Docker (785.394 byte per 3.637 richieste). Un'istanza ferma, con il solo controllo del monitor, scrive ≈ 670 KB al giorno (1.047.639 byte in 37,7 ore). 50.000 richieste al giorno ≈ 11 MB; il json-file ruota a 50 MB × 5: finendo il vecchio file a ogni giro, si perdono righe solo con più di 200 MB in 5 minuti (≈ 900.000 richieste), un caso che non c'è.
- **Il costo del helper**: `compose-helper … state` (con `docker compose ps`) 0,35-0,43 s, ≈ 0,4 s di CPU; avvio di Python e import del helper 0,10-0,12 s; 64 KB letti da un offset 16 ms. Una lettura del log per offset costa ≈ 0,13 s: **corretto in 19.7.2** (≈ 0,9% di un core per 20 istanze, non 2%).
- **Connessioni sotto carico, 4 worker** (`/web/login`, 20 s per prova): le `ESTABLISHED` sulla porta pubblicata (il lato del proxy di Docker) seguono le richieste in corso, code comprese: 2 client → 2 (mediana), 4 → 4, 8 → 10, 16 → 19; ≈ 195 richieste al secondo con 4 worker. Il lato verso il contenitore (porta 8069) non compare nella tabella della macchina: si conta la porta pubblicata. Quindi worker occupati = min(connessioni, `workers`), e «tutti occupati» quando le connessioni sono almeno `workers`.
- **`/proc/net/tcp` costa troppo sotto carico**: a 195 richieste al secondo ha 15.903 righe (le `TIME_WAIT`); leggerlo costa 45 ms da solo e fino a 440 ms in un processo carico. `ss -Htn state established` filtra nel kernel: 29 ms a chiamata. **Corretto in 19.1**: una chiamata `ss` ogni 5 s per tutte le istanze, contata per porta.
- **Il cluster della macchina**: il campionatore leggeva `system-postgresql.slice`, il cluster è in `omniacli_machine_db.slice` (`machinedb.SLICE`): corretto nella fetta 1.

## Fetta 2: richieste e log (27/11/2026, lab3 e lab4)

Costruita sul branch `monitor-s2`. Codice: `odoolog.py` (cosa si conta), `instancelog.py` (la lettura in coda e il cursore), `composehelper.log_read` (i byte del json-file), `instancemonitor.py` (le cause, le tile, le sezioni), `monitor.Monitor.instance_causes` (gli avvisi).

- **Il helper `log-read`** prende servizio, id del container, inode e offset: mai un percorso. Trova il container con `docker ps` sulle etichette di compose (`project.working_dir`, `service`) e il json-file con `docker inspect`, legge al più 4 MiB fino all'ultima riga intera. Un file ruotato (`.1`…`.20`) si finisce dal vecchio inode; uno compresso, un container rifatto o un file accorciato sono un buco detto sulla pagina. Costo misurato su lab3: **0,20 s di CPU a lettura** (due chiamate a `docker` più la lettura), non 0,13 s: 20 istanze ogni 300 s fanno ≈ 1,3% di un core.
- **Le righe d'accesso vere**: 19 su lab3 sotto carico; 16, 17 e 20 dalle immagini `odoo:16.0`, `17.0` e `20.0` senza database (database «?»). La 20 scrive l'id corto della sessione al posto del secondo campo e il tempo SQL intero quando è 0. La 19 sotto carico non ha mostrato `#modello.metodo` su `/json/2/`: il lettore lo toglie comunque dal percorso.
- **Il tempo di una richiesta** è tempo SQL più il resto. Non si contano il websocket (101) e il longpolling della 16: durano quanto resta aperto il browser.
- **Le cause**: «Lenta» e «Errori» chiedono almeno 20 richieste nelle tre campate dell'ultima lettura. Un'istanza quasi ferma, con 2 richieste lente su 3, non è lenta. «1 su 20» è il 95° percentile letto nelle classi, preso strettamente oltre il bordo: con 5 richieste su 100 oltre 5 s, «1 su 20 oltre» dice 5 s, non 0,1 s.
- **«Database fermato per memoria»** si legge nel log di Odoo: «out of memory», «crash of another server process», «recovery mode». Vale per ogni posto del database. Il `memory.events` del servizio `db` non è letto in questa fetta.
- **Avvisi**: un'istanza entra in una causa quando l'ha in una lettura ed esce dopo 3 letture senza (15 minuti). Si scrive solo quando qualcuna entra: un messaggio per causa, con tutte le istanze dentro e le nuove segnate. Il workspace riceve solo le sue istanze, Opzioni › Monitoraggio tutte. L'evento per i webhook è `instances.cause`. Quando una causa finisce non parte nessun messaggio.
- **Dopo la revisione** (27/11/2026):
  - si legge solo il container del servizio (`oneoff=False`, non l'anteprima `next_name`), mai quello di un `compose run`;
  - `/web/health` e il controllo del monitor (`/web/login?omniacli-monitor=1`) non sono richieste;
  - non si conta come avviso il WARNING di Odoo «called ignoring args» (`odoo.http`, scritto per ogni argomento che una route non prende). I 7.401 avvisi di lab3 erano questo: la raffica di prova passava `?pad=` a `/web/health`;
  - nei percorsi niente testo del cliente: nomi di file `<f>`, slug `<x>-<n>`, parole di 16 lettere `<x>`, `%xx` decodificato prima;
  - le cause si salvano appena cambiano, e un'istanza non più misurata esce da tutte;
  - una rincorsa di ore tiene intere le sue campate fino a quando la lettura le ha passate.
- **Un job su un'istanza compose** legge prima il suo log per intero, oltre i 20 MB del giro normale, per al più 60 s, 15 s a lettura; una lettura lasciata a metà chiude il suo helper.

Prove sul lab (27/11/2026):

- **Criterio 1**, su lab3 `acme-odoo`. Carico di 21 minuti: 1 richiesta su 19 oltre 3 s (un'azione server che dorme 3,4 s) e 1 su 50 con 500 (un'azione che divide per zero), dall'API JSON-2. Cominciato alle 06:28:50, «Da guardare» alle 06:33:50, cioè 5 minuti dopo. Le righe: «1 su 20 oltre 3,07 s … supera 3 s» e «Il 2% delle richieste non riesce (72 su 3593)».
- **0 righe perse**. Durante il carico ci sono state una rotazione di Docker (7.400 richieste con 7.000 caratteri di query string: il json-file a 50 MB è diventato `.1`) e un «Aggiorna» (pull, update dei moduli, riavvio). Il conteggio del monitor per ogni campata di 5 minuti coincide con le righe d'accesso del log di Odoo (`docker logs`). Dalle 06:30 alle 06:50 le campate hanno 3.691, 10.989, 3.749 e 3.599 righe, 22.028 in tutto, uguali. Per leggere i 52 MB della rotazione sono servite 3 letture: il tetto di 20 MB a giro ritarda i numeri, non li perde.
- **Avvisi raggruppati**: `acme-odoo` e `acme-zipt` lente e con errori nella stessa lettura. Sono partite 2 e-mail (Mailpit di lab3), una «2 istanze sono lente» e una «2 istanze non riescono a rispondere a delle richieste», ciascuna con le due istanze. Una terza e-mail è partita 20 minuti dopo, solo per `acme-odoo` «(nuova)». La raffica di richieste veloci aveva diluito la quota delle lente sotto 1 su 20 per 3 letture, quindi `acme-odoo` era uscita dalla causa e poi ci è rientrata. Non è una ripetizione ogni 5 minuti.
- **Criterio 12**, su lab4: un'istanza venv con `logfile` vuoto mostra «—» nel tempo di risposta e la frase di §6.2.

## Fetta 3: posta (28/11/2026, lab4)

Costruita sul branch `monitor-s3`. Codice: `instancemail.py` (la lettura, la causa, la sezione), `composehelper.MAIL_QUERY` e l'azione `pg-mail` del helper, `stagingmail.held`, `monitor.py` (l'avviso «mail»).

- **La query** è una sola, fissa, in `BEGIN TRANSACTION READ ONLY` con `statement_timeout` 10 s. Il chiamante passa solo un numero (l'`id` da cui leggere). `mail_message` si legge solo per chiave primaria: `max(id)`, `id >` un limite, e nelle prime letture il limite trovato con `ORDER BY id DESC LIMIT 1` sui messaggi di più di 2 giorni. `mail_mail` solo in aggregati. Un test conta le letture e fallisce se una non è limitata.
- **Gli ultimi 2 giorni si rileggono a ogni lettura**: il limite è l'`id` più alto letto 2 giorni prima. Una campata di 5 minuti si riscrive solo quando il suo conto cambia (anche a 0, se i messaggi sono stati cancellati).
- **Odoo 16**: le ricevute escludono i messaggi con una riga di `mail_mail`. Dalla 17 le uscite sono `email_outgoing`.
- **Le non partite si contano per classe del motivo**, mai con il suo testo: `smtp_auth` (il server rifiuta l'accesso), `smtp_connect` (il server non risponde), `smtp_refused` (il server rifiuta i messaggi), `recipient`, `sender`, `spam`, `other`. Odoo 19 lascia vuoto `failure_type` sulla mail quando la connessione al server fallisce: decidono le parole del motivo (misurato su lab4: un 535 era finito fra i messaggi rifiutati). Il testo del motivo non esce mai dal database, per nessuna persona: chi deve leggerlo lo trova in Odoo, Impostazioni › Tecnico › E-mail, ed è quello che dice il rimedio.
- **Staging**: la causa «Posta» non c'è, perché la posta è trattenuta apposta. La sezione dice «Le e-mail di una staging sono trattenute: N trattenute nelle 24 ore», con N contato nel suo Mailpit; restano solo il numero e l'ora di arrivo.
- **Costo**: 0,4-0,6 s a lettura, misurato con il helper, compreso il `docker exec` di `psql_options`. Si legge nella stessa coda del log, subito dopo.

Prove sul lab (28/11/2026, lab4, Odoo 19, modulo `mail` installato per la prova):

- **Il conto a mano**: su `acme-odoo` (database dedicato) 20 inviate, 2 respinte (da MAILER-DAEMON), 3 ricevute e 1 con un SMTP che rifiuta la password. Il monitor dice «Inviate 20 · non partite 1 · ricevute 4 · respinte 2». Il conto a mano: 20 nel Mailpit di prova e 20 righe «successfully sent» nel log; 2 righe «Routing mail … bounce»; 1 `mail_mail` in eccezione con 535; 4 `mail_message` di tipo email, cioè le 3 più 1 creata all'installazione di `mail`.
- **Un avviso solo** per due istanze con l'SMTP che rifiuta l'accesso: `acme-odoo` e `acme-mach`, quest'ultima sul cluster della macchina. È partita 1 e-mail, «2 istanze hanno e-mail non partite», con le due righe «il server di posta rifiuta l'accesso (nuova)». Nessuna e-mail nelle letture dopo, nemmeno dopo due riavvii del pannello.
- **Criterio 13**: su `acme-mach-stg-20261122`, con 6 e-mail mandate dalla staging, la sezione dice «Le e-mail di una staging sono trattenute: 6 trattenute nelle 24 ore» (il Mailpit della staging ne ha 6), e la causa Posta non c'è.

## 8. Vincoli per il dev-lead

1. Nessun demone e nessuna dipendenza. Le letture vanno in una coda sola, sfalsate.
2. Il helper non riceve mai percorsi né SQL dal chiamante, e non interpreta il log.
3. Nessuna query senza intervallo di `id`, aggregato o `LIMIT`: un test le conta.
4. Una fonte che manca è `null` con il motivo, mai 0.
5. Negli aggregati 0 IP, query string, indirizzi e testi del log, e per P2 0 host, percorsi e macchine: un test li cerca per valore, anche attraverso il centrale.
6. Ogni fetta aggiunge i suoi campi alla stessa route REST e allo stesso strumento MCP.

## 9. Fette (lab3 e lab4, pannello dal worktree)

0. **Misura** (0,5 g): righe vere di 16-20; byte di log al giorno; costo del helper; `docker logs --since` su 250 MB; connessioni sotto carico con 4 worker.
1. **Carico e verdetto** (3 g): Odoo e Database separati, slice del cluster per P1, 14 giorni, «Risponde», Memoria, REST e MCP.
   - *Prova*: criteri 4, 6-8, 10 e 11 di UX-018.
   - *Si annulla*: con il rilascio di prima.
2. **Richieste e log** (3,5 g).
   - *Prova*: criteri 1, 2 e 12. Un `logrotate` e un aggiornamento a metà del carico perdono 0 righe.
3. **Posta** (1,5 g).
   - *Prova*: 20 e-mail, 2 respinte, 1 SMTP sbagliato: i numeri coincidono con il conto a mano. Criterio 13.
4. **Spazio** (1 g).
   - *Prova*: con 1 GB di allegati la previsione si muove; la query sta sotto 1 s.
5. **Spostamento** (1 g).
   - *Prova*: lab3 → lab4 → lab3, 14 giorni interi.

In tutto ≈ 10,5 g. Le fette 1-2 (6,5 g) rispondono a «sta bene? se no, perché».

## 10. Da decidere con Carlo (la raccomandata per prima)

1. **Inviate: dal log o dal database?**
   - Raccomandata: **dal log**. Esempio: 340 e-mail al giorno, 200 con `auto_delete` (reset password, modelli): il database ne vede 140, il log 340.
   - Alternativa: database, con «Inviate» sottostimato.
2. **Freschezza con la scheda aperta.**
   - Raccomandata: **ogni minuto per l'istanza guardata**: +0,15 s di CPU al minuto, e il criterio 1 («entro 2 minuti») tiene.
   - Alternativa: 5 minuti fissi. Il verdetto arriva fino a 5 minuti dopo, e il criterio 1 va riscritto.
3. **La storia segue l'istanza.**
   - Raccomandata: **sì** (≤ 5 MB). Esempio: con `acme-odoo` spostata il 10/12, il 12/12 il grafico di 7 giorni è intero.
   - Alternativa: il grafico parte dal 10/12.
4. **Quali istanze.**
   - Raccomandata: **produzioni e staging accese**. Esempio: 12 più 6 fanno ≈ 1,8% di un core.
   - Alternativa: solo produzioni, ≈ 1,2%.
5. **Avvisi per le cause nuove** (lenta, errori, posta).
   - Raccomandata: **dopo, non qui**. Esempio: con l'SMTP giù, 12 produzioni danno 12 avvisi in una volta.
   - Alternativa: subito, +1 g.

## Non verificato

- Che un worker ucciso da `limit_time_real` lasci una riga ERROR. Senza `werkzeug`, «non riuscite» lo perde; oltre l'1% si riapre C.
- 2,2 MB: stima (il costo del log è misurato, fetta 0).
- Le righe d'accesso della 18 su un'istanza vera: sui lab non c'è l'immagine; vale la lettura dei sorgenti. Quelle di 16, 17 e 20 sono misurate (fetta 2, senza database).

## 11. Scartate

- **B.** Tre demoni, ≈ 300 MB di RAM, e un'autorizzazione accanto al registro NIS 2.
- **C.** Senza host né tempo oggi. Aggiungerli riscrive ogni sito (regola 22), e lascia fuori i nginx non nostri.
- **Un JSON riscritto intero per istanza**: ≈ 3,5 GB di scritture al giorno per 20 istanze.

## Tracciabilità

UX-018 §9, §11, §14 e «Non verificato»; AD-003, AD-014, AD-017 (17.2, fetta 3), AD-018. Codice: `metrics.py`, `metricshistory.py`, `monitor.py`, `logtail.py`, `ops.compose_log`, `pgreport.py`, `composehelper.py`, `workspaceweb.py`, `workspacehelper.py`, `machinedb.py`, `stacktemplates.py`, `consumer.py`, `audit.py`. Odoo 16-19: `netsvc.py`, `service/server.py`, `service/model.py`, `mail_message.py`, `mail_mail.py`, `mail_thread.py`.

## Le risposte di Carlo (26/11/2026)

Stato: **deciso**.

- UX-018 D-N1, la soglia di «lenta»: fissa, metà delle richieste oltre 1 s o 1 su 20 oltre 3 s, negli ultimi 15 minuti.
- UX-018 D-N2, il periodo predefinito: 24 ore.
- UX-018 D-N3, la scheda Monitoraggio: sempre visibile.
- §10.1, le e-mail inviate: dal log.
- §10.2, la freschezza: **ogni 5 minuti fissi**, anche con la scheda aperta (contro la raccomandata ogni minuto). Il criterio 1 di UX-018 diventa «entro 6 minuti».
- §10.3, la storia segue l'istanza che si sposta: sì.
- §10.4, quali istanze: produzioni e staging accese.
- §10.5, gli avvisi per le cause nuove (lenta, errori, posta): **subito**, nella stessa build (contro la raccomandata «dopo»), raggruppati per causa così che un SMTP giù su 12 produzioni sia un avviso solo; ≈ +1 g.
