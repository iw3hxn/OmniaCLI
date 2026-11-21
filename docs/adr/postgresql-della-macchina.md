# AD-001…AD-007 — PostgreSQL della macchina

Stato: **accettato il 21/11/2026, nella forma mista** (Carlo, dopo i costi di §0: «va bene il misto»). Il cluster
della macchina è il default per le istanze nuove (clienti piccoli, staging, test); il database dedicato di oggi resta
un'opzione per il cliente grande o delicato, con ripristino a un minuto e replica solo suoi; le istanze esistenti restano
dedicate e si spostano a richiesta con un job annullabile (AD-007). Si costruisce nell'ordine di §9. Codice letto alla revisione 57a005c (`carlo_dev_2026`).
Origine: Carlo, 21/11/2026, UX-008 D2: «Farei un Postgresql per Macchina ma attenzione che la singola istanza potrebbe far uso di un Postgresql su un altra macchina. Poi va considerato il caso di Odoo19 che può montare 2 database in cluster». Riferimento: la scheda PostgreSQL del pannello di riferimento (schermate 04-05 del 21/11/2026).

## 0. Il costo della decisione, in numeri (da confermare)

Il caso d'esempio è una macchina come ber (8 CPU, 15 GB), con 3 clienti e 9 database (produzione, test e una staging ciascuno): 60 GB in tutto, di cui 5 GB del cliente A. I numeri sono un esempio; le misure vere si prendono nei passi su ber (§9).

| Cosa | Oggi (un cluster per ambiente) | Cluster della macchina |
|---|---|---|
| Un backend di A ucciso dall'OOM | si ferma il database di A | PostgreSQL chiude **tutte** le connessioni e fa il crash recovery: le 9 istanze perdono il database per un tempo da misurare (secondi o minuti) |
| Ripristino di A a un minuto | base e WAL di A: circa 5 GB da estrarre | base e WAL **del cluster**: circa 60 GB estratti in un cluster temporaneo (12 volte tanto) e il WAL di tutti rigiocato, poi dump e restore dei soli 5 GB |
| Ripristino a un minuto acceso per A soltanto | sì | no: `archive_mode` vale per il cluster, quindi si archivia il WAL dei 9 database |
| Replica in streaming per A soltanto | sì, 5 GB | no: si replicano tutti i 60 GB, con i dati di B e C sull'altra macchina |
| Un terminale di Odoo di A (`psql -l`) | vede i suoi database | vede i **nomi** dei 9 database, non i dati |
| Una falla di PostgreSQL che porta un ruolo a superuser | apre 1 database | apre 9 database |
| Riavvio per un parametro come `shared_buffers` | si ferma 1 istanza | si fermano le 9 istanze insieme |
| Memoria condivisa | 9 × 128 MB separati (i valori di base misurati su ber: `shared_buffers` 128MB) | una sola, per esempio 1 GB che ogni database può usare per intero (o vedersi portare via) |
| Flussi di WAL da archiviare | uno per ogni ambiente con il ripristino acceso | uno solo |

Correzione a UX-008 D2: l'argomento «6 database × 2 GB = 12 GB su 8 GB» vale per i cluster separati. Un cluster della macchina ha **una sola** `shared_buffers` per tutti i database, quindi sulla memoria il modello per macchina vince.

Per Carlo: se la riga dell'OOM o quella del ripristino a un minuto non sono accettabili, l'alternativa è il cluster per workspace (AD-001, opzione C), che le chiude tutte e due.

## AD-001 — Un cluster PostgreSQL per macchina, in un container di OmniaCLI, uno per versione major

**Contesto.** Oggi ogni ambiente è un progetto compose con il suo servizio `db` `postgres:17`: `templates/odoo-18|odoo-19|odooplm-20/compose.yaml`, `"db_service": "db"` in `template.json`, ruolo `odoo` creato da `dbinit/` (`workspacehelper.DB_ROLE_SCRIPT`). Le istanze agganciate (venv/systemd) usano il PostgreSQL di sistema o uno remoto (`odooconf.db_connection`, `pgreport.where`: service/local/remote). Due vincoli del codice pesano sulla scelta:
- il firewall dei workspace scarta quello che i container mandano alla macchina (catena `OMNIACLI-WS-IN`) e agli indirizzi privati (catena `OMNIACLI-WS-FWD`): un PostgreSQL in ascolto in TCP sulla macchina non è raggiungibile dai container;
- `provision.sh` installa `unattended-upgrades`.

**Opzioni considerate.**
- A. **Cluster della macchina in un container** (`postgres:<major>`, progetto compose di root fuori dai workspace), raggiunto dal socket Unix.
- B. Cluster della macchina dai pacchetti apt (systemd, `pg_lsclusters`). Gli aggiornamenti automatici riavviano PostgreSQL a un'ora qualunque, cioè tutti i clienti insieme senza preavviso. Serve comunque un passaggio attraverso il firewall dei container. Sulle macchine agganciate si scontra con il PostgreSQL di sistema del cliente.
- C. Cluster per workspace: un `postgres` per cliente, condiviso da produzione, test e staging. Tiene isolamento, disco, memoria, ripristino a un minuto e replica per cliente, ma non è quello che Carlo ha scelto e non è una scheda della macchina come nel riferimento.
- D. Oggi più una regolazione per istanza (UX-008 §6.2): 9 regolazioni e 9 memorie condivise, e Carlo l'ha esclusa.

**Decisione.** A. Per ogni macchina c'è un cluster per ogni major che i template chiedono; oggi è uno solo, la 17. Il modo **dedicato** di oggi (il servizio `db` del progetto) resta, per le istanze esistenti e per chi ha bisogno dell'isolamento di §0.

**Motivazione.** È la stessa immagine e lo stesso modo di gestirla di oggi (`exec` del helper, `pg_basebackup` nel container, irrigidimento `read_only`/`cap_drop`). Docker parte dopo i dischi dei workspace (drop-in già presente), quindi parte dopo i tablespace di AD-002. L'aggiornamento avviene solo quando un admin cambia il tag dell'immagine. Si toglie con `compose down` più la cartella, e ber lo chiede. Il socket Unix evita reti condivise fra clienti, eccezioni al firewall e porte: un socket si raggiunge anche da un mount in sola lettura (dedotto dal kernel, da provare al passo 3).

**Contratto d'invarianti** (quello che un cluster condiviso porta con sé):

| Invariante | Vale nel nostro dominio? |
|---|---|
| Un backend che va in crash fa ripartire tutte le connessioni del cluster | **No.** Oggi il guasto resta nel workspace; qui ferma 9 istanze (§0). Si attenua con i limiti per ruolo di AD-002, ma non si toglie |
| WAL, archivio e replica valgono per l'intero cluster | **No** per il ripristino e la replica di un solo cliente (AD-004, AD-006) |
| `pg_database` e `pg_roles` sono leggibili da ogni ruolo | **No** per la riservatezza dei nomi: 9 nomi visibili. Si accetta, scritto qui |
| Il superuser del cluster è superuser di tutti i database | Sì, se il superuser lo usa solo il helper di root |
| `max_connections` e `shared_buffers` sono una sola risorsa | Sì, se la somma dei limiti per ruolo non supera `max_connections` (AD-005) |
| «Workspace = la parte chiusa di un cliente» (disco, slice, rete), invariante di OmniaCLI | Il disco lo tiene AD-002; memoria e CPU del database escono dalla slice del workspace (vedi Conseguenze) |

**Conseguenze.**
- Il cluster ha una sua slice systemd. La memoria del workspace (`WORKSPACE_LIMITS.memory_gb`) diventa «la memoria di Odoo»: va detto nell'interfaccia.
- Socket in una cartella di OmniaCLI (non `/var/run/postgresql`). Nessuna porta TCP finché AD-003 non la apre, su una porta libera e diversa da quella di un eventuale PostgreSQL di sistema.
- Il PostgreSQL di sistema delle istanze agganciate non si tocca. La pagina della macchina lo mostra in sola lettura: OmniaCLI lì non è superuser (README, «not a superuser: pg_lsclusters»).
- `template.json` dichiara se il template porta il suo database. La validazione di `stacktemplates.py:417`, che oggi esige il servizio `db`, accetta un template senza. `pgreport.where` ha un quarto valore, `machine`. `db_service` compare in 16 punti di 5 file.
- La versione del cluster non supera quella di `pg_dump` dell'immagine di Odoo (su ber: server 17.12, `pg_dump` 19.0 in `odoo:19.0`, va bene).

**Tracciabilità.** UX-008 §0 (riga PostgreSQL), §6.2, §13 D2 e «Decisioni prese»; nessun T/HT.

## AD-002 — Isolamento fra clienti nel cluster condiviso: ruoli, `pg_hba`, tablespace sul disco del workspace

**Contesto.** Oggi il database di un cliente sta in `data/pgdata` sul disco del suo workspace (immagine ext4 con un tetto, `disk_gb`), dentro la sua slice. I developer di un workspace hanno un terminale nel container di Odoo (`terminal`). Odoo crea database con `CREATE DATABASE … TEMPLATE <db_template>`, e `default_tablespace` non vale per `CREATE DATABASE`. Il flusso di verifica dei backup (`verify`) crea `omniacli_verify_<random>` con il ruolo di Odoo.

**Opzioni considerate.**
- A. Un tablespace per workspace sul suo disco, nessun CREATEDB fuori dai job, `pg_hba` per coppia database/ruolo.
- B. Tutto nel `PGDATA` della macchina con una quota misurata (`pg_database_size` per proprietario). Un ruolo con CREATEDB, cioè chiunque abbia un terminale, riempie il disco della macchina con `CREATE DATABASE x TEMPLATE template0`, e si fermano tutti.
- C. CREATEDB sempre e nessun tablespace: lo stesso rischio, e il disco del cliente non conta più il suo database.

**Decisione.** A.

**Motivazione.** Tiene l'invariante «il dato del cliente sta sul suo disco, con il suo tetto», e cancellare il workspace cancella i suoi dati. Riempire il disco resta un danno che il cliente fa a se stesso.

**Conseguenze (vincoli).**
1. Ruoli:
   - un ruolo di gruppo per workspace (NOLOGIN), proprietario del tablespace;
   - un ruolo di login per ambiente, membro del suo gruppo, NOSUPERUSER, NOCREATEROLE, NOREPLICATION;
   - **CREATEDB solo dentro un job di OmniaCLI**, oppure i database li crea il superuser tramite il helper, con OWNER e TABLESPACE: la scelta spetta al dev-lead.
2. `pg_hba`: una riga `local <db> <ruolo> scram-sha-256` per ambiente e un `reject` finale. `REVOKE CONNECT ON DATABASE … FROM PUBLIC` su ogni database.
3. Limiti per ruolo, che un non superuser non può cambiare: `CONNECTION LIMIT`, `temp_file_limit`, `temp_tablespaces` sul tablespace del workspace, `idle_in_transaction_session_timeout`. Niente `statement_timeout` di default: gli aggiornamenti dei moduli (`-u`) fanno ALTER lunghi.
4. Il nome del database è unico **sul cluster**: workspace, istanza e ruolo (63 caratteri al massimo). Oggi produzione e test hanno lo stesso nome in cluster diversi; i flussi che lo danno per scontato (filestore `filestore/<db>`, `link-filestore`, restore) vanno controllati.
5. Le statistiche sono filtrate sul database dell'istanza: `pg_stat_activity` per `datname`, `pg_stat_statements` per `dbid`. `pg-terminate` lo fa già (`datname = current_database()`).
6. Il superuser del cluster: la password sta in un file solo di root e la usa solo il helper; nessun ruolo di cliente la conosce, come oggi `DB_SUPER_PASSWORD`.
7. Costo accettato: i nomi dei database sono visibili a tutti i ruoli (§0).

**Tracciabilità.** UX-008 D2; README «Workspaces» (disk, limits, firewall).

## AD-003 — Database su un'altra macchina: del nostro cluster o di terzi

**Contesto.** Il collegamento fra due OmniaCLI è HTTP, eventualmente in `ssh -L` (`links.py`, `sshlinks.py`). Non c'è una rete per il traffico del database. `provision.sh` apre in ufw solo SSH, 80 e 443. Il firewall dei container scarta gli indirizzi privati. Odoo non ha un'opzione per il certificato della CA, ma libpq legge `PGSSLROOTCERT` dall'ambiente.

**Opzioni considerate.**
- A. TCP diretto con TLS verificato (CA fissata per ogni cluster), `pg_hba hostssl` per l'IP /32 della macchina client, regola ufw `from <IP>`.
- B. Attraverso il tunnel SSH del collegamento: un secondo forward da aprire per l'utente del collegamento, più latenza e un tunnel che cade e va riaperto.
- C. Una VPN fra le macchine: un componente in più da installare, gestire e togliere.

**Decisione.** A, con due casi.

| | Cluster di un altro OmniaCLI collegato (B) | Cluster di terzi (per esempio gestito dal cliente) |
|---|---|---|
| OmniaCLI configura | su B: ruolo, database e tablespace del workspace (AD-002), riga `hostssl`, ufw. Su A: `db_host`/`db_port`/`db_sslmode=verify-full`, la CA fissata | solo la connessione su A (host, porta, database, ruolo, password, sslmode, CA) |
| Regolazione | la fa l'admin di B, dalla pagina Database di B (attraverso il collegamento) | nessuna, e lo dice |
| Backup | da A con `odoo db dump` (va già in rete) | uguale |
| Ripristino a un minuto e replica | di B, sul cluster di B (AD-004, AD-006) | non offerti |
| Statistiche | `pg-report` su B, filtrate | con i soli diritti del ruolo: senza superuser alcune parti mancano, e va detto |

**Motivazione.** È il meccanismo che PostgreSQL ha già (TLS e `pg_hba`), senza componenti nuovi; si annulla togliendo una riga di `pg_hba` e una regola ufw.

**Conseguenze (vincoli).**
- Mai `0.0.0.0/0` in `pg_hba` né in ufw. Mai `sslmode` sotto `verify-full` verso una macchina nostra.
- Se il database è su un indirizzo privato, il firewall dei container ha un'eccezione per quel solo host e porta.
- **Latenza**: Odoo fa molte query per richiesta. Esempio: 200 query con 0,2 ms di andata e ritorno sono 0,04 s, con 5 ms sono 1 s in più. Il dialogo mostra il tempo di andata e ritorno misurato e avvisa sopra una soglia, da fissare con la misura del passo 7.
- Una macchina intera che usa un database remoto è un'istanza per volta, ciascuna con la sua connessione: niente «macchina senza database» come concetto a parte.

**Tracciabilità.** UX-008 D2 («un PostgreSQL su un'altra macchina»).

## AD-004 — Odoo con due database (primario e replica): cosa supporta Odoo, e cosa fa OmniaCLI

**Contesto: sorgente letto il 21/11/2026 da raw.githubusercontent.com.**
- **17.0**: `odoo/tools/config.py` e `odoo/sql_db.py` non contengono `replica`, quindi nessun supporto.
- **18.0**, `odoo/tools/config.py` righe 263-268:
  ```
  group.add_option("--db_replica_host", dest="db_replica_host", my_default=False,
                   help="specify the replica host. Specify an empty db_replica_host to use the default unix socket.")
  group.add_option("--db_replica_port", dest="db_replica_port", my_default=False,
                   help="specify the replica port", type="int")
  ```
- **18.0**, `odoo/sql_db.py` righe 847-852 (`connection_info_for`):
  ```
  for p in ('host', 'port', 'user', 'password', 'sslmode'):
      cfg = tools.config['db_' + p]
      if readonly:
          cfg = tools.config.get('db_replica_' + p) or cfg
  ```
  Esistono solo `db_replica_host` e `db_replica_port`: utente, password e sslmode della replica sono quelli del primario.
- **18.0**, `odoo/modules/registry.py` righe 166-167 e 1015-1027:
  ```
  if config['db_replica_host'] is not False or config['test_enable']:
      self._db_readonly = odoo.sql_db.db_connect(db_name, readonly=True)
  ```
  Se la replica non risponde (`psycopg2.OperationalError`), Odoo usa il primario per `_REPLICA_RETRY_TIME = 20 * 60` secondi (riga 65).
- **18.0**, `odoo/http.py` righe 2234-2247: le route `readonly` provano prima il cursore in sola lettura; su `ReadOnlySqlTransaction` riprovano in lettura e scrittura. Il default è `readonly` per le route `auth='none'` (riga 890). Riga 1768: le sequenze vanno sempre sul primario («Sequence data is not replicated»). `odoo/api.py` riga 436: `@api.readonly` per i metodi chiamati da RPC.
- **19.0**, `odoo/tools/config.py` righe 387-392: le stesse opzioni, con `env_name='PGHOST_REPLICA'` e `'PGPORT_REPLICA'`. Righe 685-706: un `db_replica_host` vuoto non vuol più dire «socket locale» ma diventa `dev_mode=replica`, con un DeprecationWarning («Since 19.0, an empty db_replica_host was the 18.0 way…»).
- **19.0**, `odoo/orm/registry.py` riga 280: `if config['db_replica_host'] or config['test_enable'] or 'replica' in config['dev_mode']`, con lo stesso ritorno al primario dopo 20 minuti (riga 74). `odoo/http.py` righe 2374-2406 seguono la stessa logica, e senza primario rispondono 503.

In sintesi, i «due database in cluster» sono **un primario più una replica in sola lettura per scalare le letture**, dalla **18.0** e non dalla 19. Non c'è failover: se il primario cade, Odoo si ferma. Oggi OmniaCLI mette `db_replica_host` e `db_replica_port` fra le opzioni bloccate (`stacktemplates.LOCKED_OPTIONS`, riga 157), quindi le scrive solo OmniaCLI.

**Opzioni considerate.**
- A. Replica in streaming fisica del **cluster** (della macchina o dedicato) su un'altra macchina, con `db_replica_host`/`db_replica_port` nell'odoo.conf. La promozione è manuale, con un job.
- B. Replica logica di un solo database: i DDL non si replicano (ogni `-u` che aggiunge una colonna la rompe) e nemmeno le sequenze. Non regge con Odoo.
- C. Failover automatico: con due nodi non c'è quorum e si rischia lo split-brain; servirebbe un terzo nodo e un gestore del cluster. È fuori misura.

**Decisione.** A, come **ultimo passo** (§9): la replica è una proprietà di un cluster, non di un'istanza. Per un solo cliente si usa un cluster dedicato.

**Motivazione.** È l'unica forma che Odoo usa davvero (letture sulla replica, scritture sul primario). La promozione manuale evita lo split-brain senza aggiungere nodi.

**Conseguenze (vincoli).**
- OmniaCLI non scrive **mai** un `db_replica_host` vuoto (18 e 19 lo leggono in modo diverso).
- Ruolo di replica dedicato, `hostssl replication` per l'IP /32, uno slot di replica.
- `max_wal_senders` e `wal_level=replica` (il default della 17) chiedono un riavvio se cambiano.
- Il ritardo della replica si mostra in secondi.
- La promozione è un job: si spegne il vecchio primario prima di promuovere, si riscrive `db_host` e si riavvia Odoo.
- Una replica del cluster della macchina contiene tutti i clienti (§0).
- Il template odooplm-20: con quale ramo di Odoo gira non l'ho letto.

**Tracciabilità.** UX-008 D2 («Odoo19 che può montare 2 database in cluster»).

## AD-005 — Regolazione del cluster: sull'hardware, riavvio letto da PostgreSQL, applicato senza fermare tutti a sorpresa

**Contesto.** UX-008 §6.2 metteva la regolazione nell'istanza. Il riferimento ha «Apply basic optimization», «Auto-tune» sull'hardware, i parametri con il default e il badge «restart».

**Opzioni considerate.**
- A. Regolazione **del cluster**, nella pagina della macchina (o nella scheda Database dell'istanza, se il cluster è dedicato).
- B. Regolazione per istanza (UX-008 §6.2): esclusa da Carlo.
- C. Parametri per database (`ALTER DATABASE SET`): valgono solo i parametri di sessione, non la memoria condivisa.

**Decisione.** A.

**Conseguenze (vincoli).**
1. **Base del calcolo**: la «memoria per il database» della macchina, un valore della macchina. Il default è una quota della RAM al netto di quello che i workspace riservano a Odoo; il calcolo lo scrive in una riga. Gli altri input sono i core e il tipo di disco.
2. Esempio su 15 GB con 4 GB per il database e 200 connessioni:
   - `shared_buffers` 1 GB;
   - `effective_cache_size` 3 GB;
   - `maintenance_work_mem` 256 MB;
   - `work_mem` 8 MB;
   - `random_page_cost` 1.1.
   Le formule le fissa il dev-lead; il calcolo si vede prima di applicarlo.
3. **Riavvio sì o no** si legge da `pg_settings.context = 'postmaster'` e `pending_restart`, mai da una lista scritta a mano.
4. I parametri che basta ricaricare valgono subito (`pg_reload_conf`). Quelli che chiedono un riavvio restano «in attesa di riavvio» finché l'admin della macchina non sceglie **adesso** oppure **alle <ora>** (scheduler). La conferma elenca le istanze che si fermano: «9 istanze perdono il database per qualche secondo».
5. Riavvio veloce: prima un `CHECKPOINT`, poi un arresto `fast`. Si misura su ber quante richieste di Odoo falliscono dopo (passo 2).
6. Lo regola solo l'admin della macchina; l'admin del workspace vede in sola lettura «cluster della macchina X, regolato il …».
7. La somma dei `CONNECTION LIMIT` dei ruoli non supera `max_connections` meno una riserva: lo si controlla quando nasce un ambiente.
8. I parametri del ripristino a un minuto (`archive_*`) e della replica sono del loro proprietario: la regolazione non li mostra e un «ripristina i valori di base» non li azzera.
9. Le statistiche delle query (`pg_stat_statements`, in `shared_preload_libraries`) le accende «Applica l'ottimizzazione di base», e chiedono un riavvio.

**Tracciabilità.** UX-008 §6.2 (da riscrivere: pagina della macchina, non dell'istanza), criterio 7.

## AD-006 — Ripristino a un minuto, backup, staging e verifica sul cluster condiviso

**Contesto.** `pitrhelper.py` lavora sul cluster dell'ambiente: archivio in `data/wal`, `pg_basebackup -Ft -z`, e il restore **sostituisce il `pgdata`** della staging di destinazione. Su ber, il 21/11: un database `odoo` di 27 MB, 19 segmenti archiviati (296 MB) del 20/11, una base del 20/11 alle 10:40 da 7,5 MB. Backup, staging e verifica passano dal container di Odoo (`odoo db dump/load`, `neutralize`, `verify`).

**Opzioni considerate.**
- A. Archivio e basi **del cluster**. Il ripristino di un database a un minuto passa da un **cluster temporaneo**: un `postgres` dedicato, cioè il servizio `db` di oggi, riportato a quel minuto. Poi si fa dump e restore del solo database nella staging, sul cluster della macchina. Infine il cluster temporaneo si cancella.
- B. Uno strumento esterno che ripristina database scelti: riduce il disco, non il WAL da rigiocare. È uno strumento in più (`pitrhelper` oggi dice «no other tool»).
- C. Ripristino a un minuto solo sui cluster dedicati: semplice, ma il cluster della macchina resterebbe senza.

**Decisione.** A. Chi vuole il ripristino isolato del solo cliente (§0) sta su un cluster dedicato, dove resta il flusso di oggi.

**Conseguenze (vincoli).**
- Il cluster temporaneo è di root, fuori dai workspace, sul disco della macchina, e si cancella in ogni caso. Contiene per qualche minuto i dati di tutti i clienti: va detto.
- Il dialogo di ripristino dice lo spazio e il tempo: la base intera, non solo il database.
- Il WAL e le basi sul disco della macchina hanno un loro budget e un avviso; `KEEP_BASES` vale per il cluster.
- Backup e staging restano `odoo db dump` dal container di Odoo e non cambiano, salvo la creazione del database (AD-002).
- La verifica della domenica (`verify`) gira **nel cluster temporaneo**, non in quello condiviso: non usa la memoria condivisa dei clienti e non ha bisogno di CREATEDB.
- Il ripristino a un minuto del cluster della macchina lo accende e lo spegne solo l'admin della macchina.

**Tracciabilità.** README «Back to a minute»; UX-008 §6.2 (HIDE dei parametri del ripristino).

## AD-007 — Migrazione: i dedicati restano, il cluster della macchina vale per le nuove, il passaggio è un job a richiesta

**Contesto.** Su ber c'è `odoo19-odoo` (produzione) con il ripristino a un minuto acceso. `instance.json` registra il template e la sua versione. Le modifiche al core di Matteo vanno tenute piccole, per poterle proporre.

**Opzioni considerate.**
- A. Le esistenti restano dedicate e le nuove nascono sul cluster della macchina; spostarle è un job a richiesta.
- B. Migrazione di tutte in blocco: un fermo per ogni cliente e la storia del ripristino a un minuto persa tutta insieme.
- C. Migrazione senza fermo con la replica logica: si rompe ai DDL (AD-004 B).

**Decisione.** A.

**Conseguenze (vincoli).**
1. Il `compose.yaml` di un'istanza esistente non cambia di un byte. I template di oggi restano il modo dedicato; il modo «macchina» è una variante senza il servizio `db`, con una versione nuova. Nella Nuova istanza, «database dedicato» è un'opzione avanzata.
2. Il job «Sposta sul cluster della macchina»:
   - ferma Odoo;
   - fa `pg_dump -Fc` dal servizio dedicato;
   - crea il database nel tablespace del workspace;
   - fa `pg_restore -j <core>`;
   - verifica (per esempio `verify_check`) e riparte con la nuova connessione.
   Il vecchio `pgdata` resta finché l'admin non conferma, perché è l'annullamento. Il fermo stimato si dice prima, in base alla dimensione del database, misurata.
3. Il job inverso («Torna a un database dedicato») esiste dallo stesso passo: è l'annullamento.
4. La storia del ripristino a un minuto di prima dello spostamento resta raggiungibile dall'archivio vecchio finché lo si tiene, e lo si dice.
5. La memoria del workspace che prima copriva anche il suo database si rivede dopo lo spostamento (AD-001).

**Tracciabilità.** UX-008 «Decisioni prese»; memoria ber (tutto annullabile).

## 8. Vincoli non funzionali (per il dev-lead)

1. **Isolamento**: nessun ruolo si collega al database di un altro; le statistiche sono filtrate sul database; niente CREATEDB fuori dai job; un solo superuser, del helper. Una prova automatica: dal container di A, `psql` verso il database di B deve fallire.
2. **Disponibilità**: un riavvio del cluster lo decide solo l'admin della macchina, con l'elenco delle istanze e l'ora scelta. Il crash per OOM (§0) si attenua con i limiti per ruolo e la slice del cluster: il tempo di ripresa misurato va nell'interfaccia.
3. **Risorse**: la somma dei limiti di connessione non supera `max_connections`; il budget di memoria è esplicito; il disco del cliente porta i suoi dati (tablespace); WAL e archivio hanno un budget della macchina.
4. **Rete**: un database remoto passa solo con TLS `verify-full` e un /32; il tempo di andata e ritorno è misurato e mostrato; il firewall dei container riceve un'eccezione per host e porta, mai per una rete.
5. **Compatibilità**: un cluster per major; il major non supera quello di `pg_dump` dell'immagine di Odoo; `db_replica_host` mai vuoto. Il modo dedicato resta il codice di oggi, così la proposta di merge resta piccola.

## 9. Ordine di costruzione (ogni passo è rilasciabile e provato su ber; ogni passo si annulla)

1. **Pagina Database della macchina in sola lettura**: i cluster presenti (i dedicati di ogni ambiente, il PostgreSQL di sistema con `pg_lsclusters`) e i loro parametri da `pg_settings`. Prova su ber: si legge il cluster di `odoo19-odoo` (`shared_buffers` 128MB, `max_connections` 100, `archive_mode` on). Non installa niente.
2. **Motore di regolazione** (AD-005) applicato ai **cluster dedicati** di oggi: calcolo, reload contro riavvio da `pg_settings`, riavvio a un'ora scelta. Prova su ber: regolare il cluster di `odoo19-odoo` lasciando intatti i parametri `archive_*`, misurare la durata del riavvio e le richieste di Odoo fallite. Annullamento: `ALTER SYSTEM RESET` dei soli parametri toccati.
3. **Cluster della macchina** (AD-001): helper per crearlo e toglierlo, slice, socket, nessuna porta. Prova su ber: lo crea, lo regola, lo toglie; dopo, nessun container, cartella, unit o immagine in più (ROLLBACK.md aggiornato).
4. **Template in modo macchina e isolamento** (AD-002): un'istanza nuova nel workspace `odoo19` su ber, con staging, backup, verifica, neutralize, statistiche e Stop di una query. Prova di isolamento con un secondo workspace: il `psql` incrociato fallisce e `psql -l` mostra i nomi (costo atteso). Annullamento: cancellare l'istanza, poi `DROP TABLESPACE` e i ruoli.
5. **Ripristino a un minuto del cluster della macchina** (AD-006), con il cluster temporaneo. Prova su ber: ripristino di un database a un minuto, misurando tempo, disco temporaneo e la cancellazione finale.
6. **Spostamento dedicato → macchina e ritorno** (AD-007). Prova su ber: su una **staging** di `odoo19-odoo`, non sulla produzione con il ripristino acceso; fermo misurato; poi il ritorno.
7. **Database remoto da un altro OmniaCLI** (AD-003). Prova: Odoo su inaria e database su ber (uscita da inaria verso la porta di ber, ufw di ber `from <IP di inaria>`); misurare il tempo di andata e ritorno e quello di una pagina. Annullamento: riga di `pg_hba`, regola ufw, ruolo e database.
8. **Database di terzi**: solo la connessione. Prova: il cluster di ber usato da inaria come se fosse di terzi, senza collegamento.
9. **Replica** (AD-004): prima su ber, con un secondo container replica sulla stessa macchina (prova la configurazione di Odoo e il ritorno al primario dopo 20 minuti), poi fra due macchine. Inaria apre solo la porta 22528 dal fornitore: la replica con primario su inaria chiede di aprirne un'altra lì.

## Non verificato

- Che un socket Unix in un mount in sola lettura si raggiunga dal container di Odoo (`read_only: true`), e che l'entrypoint dell'immagine accetti come `HOST` una cartella: dedotto da come funzionano kernel e libpq, da provare al passo 3.
- Il tempo di ripresa dopo un crash per OOM o un riavvio veloce, e quante richieste di Odoo falliscono dopo (`reset()` del pool su una connessione chiusa dal server): non misurato.
- Che un ruolo con CREATEDB non possa creare un database in `pg_default` aggirando il tablespace: ragionato sul codice di `createdb` a memoria, non letto. Per questo AD-002 toglie CREATEDB fuori dai job.
- Un tablespace su un'immagine loop che si riempie: se un `fsync` fallito manda in PANIC l'intero cluster. Non provato.
- Che `pg_basebackup -Ft` con i tablespace si ripristini nel cluster temporaneo con `tablespace_map`: da provare al passo 5.
- Su quale ramo di Odoo gira il template odooplm-20, e quindi se ha la replica: non letto.
- Il supporto di `host=a,b` in `db_host` più `PGTARGETSESSIONATTRS` per seguire una replica promossa: possibile in libpq, non provato con Odoo.
- Le formule di regolazione e la quota di RAM di default sono indicazioni: nessuna misura di carico.
- Osservazione su ber, fuori dal mandato: c'è una sola base del ripristino a un minuto (20/11 alle 10:40) e nessuna del 21/11. Non ho controllato se il giro notturno sia girato.
- Su ber ho fatto solo letture via SSH (versioni, dimensioni, archivio); su nessuna macchina ho scritto niente.
