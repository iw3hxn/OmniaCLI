# AD-001…AD-007 — PostgreSQL della macchina

Stato: **accettato il 21/11/2026, nella forma mista** (Carlo, dopo i costi di §0: «va bene il misto»). Il cluster
della macchina è il default per le istanze nuove (clienti piccoli, staging, test); il database dedicato di oggi resta
un'opzione per il cliente grande o delicato, con ripristino a un minuto e replica solo suoi; le istanze esistenti restano
dedicate e si spostano a richiesta con un job annullabile (AD-007). Si costruisce nell'ordine di §9.

**Costruito al 21/11/2026 (release 2026.1.5463)**, provato su una macchina di prova: il cluster della macchina
(AD-001), l'isolamento per ruolo, database e tablespace (AD-002: il ruolo di un ambiente entra solo nel suo database),
la regolazione del cluster (AD-005), backup, ripristino e staging delle sue istanze, lo spostamento nei due sensi con
l'annullamento (AD-007: Odoo fermo 8,3 s e 4,5 s su un database di 21 MiB).
**AD-006, costruito il 22/11/2026** (`clusterpitr.py`, `workspace-helper machine-db-archive-*` e
`machine-db-recover`), provato su lab2 (cluster di 150-200 MB, cinque istanze più il database di un'altra macchina):
- **Archivio e copie complete del cluster**, nel progetto del cluster (`<storage>/.machine-db/pg17/wal`, sul disco
  vero dello storage, fuori dai dischi dei workspace, leggibile solo da root e dall'utente del cluster: contiene i dati
  di tutti). PostgreSQL ci scrive da solo, perché lo storage è montato nel suo container dov'è sulla macchina: il
  container non si rifà. Lo accende e lo spegne solo l'admin della macchina, dalla riga del cluster nella scheda
  Database. Il cluster si riavvia **solo se `archive_mode` cambia**: il dialogo elenca le istanze e i database di
  altre macchine che perdono le connessioni e lo chiede un'altra volta prima di riavviare. Misurato: **0,5 s e 0,4 s**;
  su circa 120 richieste a `/web/login` di `acme-mach` e `beta-shop` (una ogni 0,36 s) una sola ha sbagliato, la
  successiva ha risposto; l'Odoo di lab1 sul database concesso ha riaperto la connessione da solo dopo 20 s e 40 s
  (alla sua richiesta successiva) e risponde 200. Lo spegnimento non riavvia: `archive_command = /bin/true` vale
  subito, `archive_mode` torna `off` al prossimo riavvio del cluster; copie e archivio vengono cancellati.
- **Copia completa**: `pg_basebackup -Ft -z`, un `<oid>.tar.gz` per ogni tablespace di workspace; 20 MiB in 2,7 s.
  Una ogni notte. Restano le ultime N (7), le altre si cancellano con i segmenti precedenti all'inizio della più
  vecchia rimasta; mentre un ripristino è in corso non si cancella niente (provato: una copia fatta durante un
  ripristino dice «Nothing older deleted»). Provato con 2 copie da tenere: da 3 copie e 19 segmenti (379 MB) a 2 copie
  e 13 segmenti (261 MB), poi con due copie nuove a 2 copie e 4 segmenti (114 MB); il segmento più vecchio rimasto è
  sempre quello da cui parte la copia più vecchia. Il budget (20 GB) avvisa soltanto: nella riga («oltre il budget»),
  nel job della copia e nel log; non cancella mai quello che può servire a un ripristino (provato con un budget di
  0,1 GB contro 0,3 GB occupati).
- **Un database a un minuto**: un cluster temporaneo in una cartella di root accanto al progetto
  (`recovery-17-<hex>`, 700), con la copia completa estratta e l'archivio montato in sola lettura; senza rete, senza
  porta, `archive_mode` spento, 128 MB di memoria condivisa. Poi `pg_dump` di quel solo database sul disco del suo
  workspace, cancellazione del cluster temporaneo, e `pg_restore` nel database della staging sul cluster della
  macchina, con il ruolo della staging. **I tablespace** (la voce di «Non verificato»): `tablespace_map` resta
  com'è; ogni `<oid>.tar.gz` si estrae sotto una cartella del cluster temporaneo che il suo container monta al posto
  dello storage, quindi il percorso vero porta ai file estratti e i tablespace veri non sono mai montati. Un
  tablespace creato dopo la copia completa e prima del minuto chiesto: PostgreSQL si ferma chiedendo la cartella,
  che viene creata nella cartella temporanea, e riparte (provato: `CREATE TABLESPACE` fatto dopo l'ultima copia
  completa, ripristino al minuto successivo riuscito con una cartella creata e un avvio in più del cluster
  temporaneo: replay 2,4 s invece di 1,3 s).
- **Si cancella in ogni caso**, provato su lab2 guardando ogni volta cartella, container, rete, volumi di docker e
  dump (prima 0, a metà 1 cartella e 1 container, dopo 0):
  - alla fine: la cartella e il container spariscono prima che la staging venga scritta;
  - a un errore: container ucciso a metà del replay; il job fallisce con una frase senza percorsi né nomi, il motivo
    intero è in `recovery.log` (di root) e nella riga del cluster;
  - a job fermato: il helper gestisce SIGTERM, SIGHUP e SIGINT; job finito 1,0 s dopo lo stop, helper uscito dopo
    1,3 s, lock rilasciato;
  - a pannello riavviato a metà: l'unit ha `KillMode=process`, quindi il helper sopravvive; la pulizia dell'avvio lo
    trova in corso e non tocca niente; il helper finisce da solo e cancella (19,9 s dopo il riavvio). Il job resta
    «interrupted» e la staging resta ferma e non neutralizzata: va cancellata e richiesta;
  - a helper ucciso con `kill -9` a metà: cartella e container restano; al riavvio di OmniaCLI la pulizia dell'avvio
    li cancella (entro pochi secondi dall'avvio) e lo scrive nel log e nel log degli accessi. La stessa pulizia la fanno il ripristino
    successivo e il giro di ogni ora, mai sotto un ripristino ancora in corso (lo tiene il suo lock).
  Il cluster temporaneo, visto a metà: cartella 700 di root (165 MB all'inizio del replay), container senza rete e
  senza porte, in sola lettura, senza volumi di docker, con l'archivio e il segreto del superuser in sola lettura.
- **Detto prima**: la pagina dell'istanza dà l'intervallo dei minuti (dal minuto dopo la copia più vecchia a quello
  prima di adesso: il ripristino archivia per prima cosa il segmento in scrittura) e il tempo atteso; all'admin della
  macchina anche lo spazio (l'intero cluster, non quel database: stimati 453-530 MB, misurati al massimo 240-340 MB) e che la
  copia contiene i dati di tutti. Rifiuta con il motivo quando la copia temporanea lascerebbe libero meno dello
  «spazio da lasciare libero» (2 GB, regolabile), quando un altro ripristino è in corso, quando il minuto è fuori
  intervallo. Il rifiuto per lo spazio è provato portando lo «spazio da lasciare libero» a 400 GB con 297 GB liberi:
  il pannello rifiuta prima di creare la staging (507), il helper rifiuta da solo se chiamato lo stesso, e niente
  viene creato. L'utente di un workspace chiede il ripristino della sua istanza come su un database dedicato (admin
  del workspace) e non legge dimensioni del cluster, percorsi né nomi di altri, nella pagina, nei rifiuti e nel job:
  provato entrando su lab2 come admin del workspace `acme` (le route della macchina rispondono 403, l'istanza di un
  altro workspace 403, il job letto da lui non ha percorsi). Il tempo atteso resta un indizio grossolano della
  dimensione del cluster: cresce con quella.
- **La staging ottenuta** contiene la riga scritta prima del minuto e non quella scritta dopo; è neutralizzata; il suo
  database è del suo ruolo, nel tablespace del suo workspace; il suo ruolo entra nel suo database e in `postgres` (come
  ogni ambiente, AD-002) e viene rifiutato da `pg_hba` sulla produzione dello stesso workspace, sul database di un
  altro cliente e su quello di un'altra macchina. Le estensioni del dump le crea il superuser: la riga di
  `pg_restore -l` di un'estensione finisce con uno spazio e `restore_list` non la riconosceva (valeva anche per lo
  spostamento di AD-007); corretto e provato con `pg_trgm` e un indice trigram.
- **Tempi misurati** (database di 20 MB): helper 14,7 s, di cui estrazione 0,5 s, replay 1,3 s, dump 0,2 s, arresto
  dell'Odoo della staging 10 s, restore 1,6 s; il job intero 126 s, di cui 90 s sono il controllo del disco del
  workspace che ogni nuova staging fa. Il tempo detto nel dialogo parte da quei 2 minuti fissi.
- **Non costruito.** La verifica della domenica **resta sul cluster condiviso** (database `<ruolo>-verify-<hex>`,
  CREATEDB per la sua durata): portarla in un cluster temporaneo vuol dire un secondo cluster vuoto raggiunto dal
  container di Odoo, con un suo ruolo e i dati sul disco della macchina invece che su quello del workspace: non è una
  modifica piccola, e non è fatta. Il database concesso a un'altra macchina è dentro le copie complete, ma non si
  ripristina a un minuto né da qui né da là. Nessuna replica, nessun ripristino dell'intero cluster sul posto.
- **Limiti.** Un solo ripristino per volta per cluster. Se l'archivio non si riesce a scrivere (disco pieno),
  PostgreSQL tiene i segmenti in `pg_wal` finché non ci riesce: la riga lo dice («non viene salvato»), ma niente lo
  ferma prima che riempia il disco. Il giro notturno delle copie complete è coperto da un test, non visto girare su
  lab2 (aspetta un giorno). Il tempo e lo spazio crescono con l'intero cluster, non con il database chiesto (§0). Se
  OmniaCLI viene fermato a metà, il helper finisce da solo ma nessuno neutralizza la staging: resta ferma, da
  cancellare e richiedere. La stima del tempo è un modello con velocità prudenti, misurato su un solo cluster piccolo.
  Il helper scrive nel database dell'istanza di destinazione che gli viene data, come `pitr-restore`: che sia una
  staging appena creata lo garantisce il pannello, non il helper.

**AD-003, costruito il 21/11/2026 nel caso «di terzi»** (release 2026.1.5464): template `odoo-18/19-remote` e
`odooplm-20-remote`, senza servizio database; Odoo raggiunge `DB_HOST:DB_PORT` in TLS con `verify-full` e la CA data
alla creazione (un template che chiede meno è rifiutato). OmniaCLI fa solo la connessione: la connessione è
controllata prima di creare, arriva all'helper sull'input (mai su una riga di comando né in un log), il firewall dei
workspace lascia passare quel solo indirizzo e porta dalla rete di quel progetto, la rimozione lascia il database dov'è
e lo dice. Regolazione, ripristino a un minuto e staging sono rifiutati con il motivo. Provato fra due macchine di
prova: connessioni TLS 1.3 viste dal database, una CA sbagliata rifiutata, backup riuscito.
**AD-003, costruito il 22/11/2026 nel caso «cluster di un altro OmniaCLI collegato»** (release 2026.1.5467): alla
creazione di un'istanza con un template `*-remote` l'admin della macchina A sceglie «sul cluster di \<link\>» e
OmniaCLI fa il resto attraverso il collegamento.
- **Su B** (`machinedb.py`, `workspace-helper machine-db-grant|ungrant|port`): il cluster comincia ad ascoltare solo
  alla prima richiesta, sulla prima porta libera da 15432 (mai una di un PostgreSQL di sistema, nemmeno fermo), in solo
  TLS, con una CA sua creata una volta (`tls/`, chiave solo di root) e un certificato server per il nome con cui A
  raggiunge B. Il container passa da `network_mode: none` a una rete sua con la porta pubblicata (solo IPv4), quindi
  viene rifatto: **0,5 s misurati**, detti e chiesti nel dialogo di A. Le istanze di B perdono le connessioni che
  tenevano aperte. Due riavvii misurati su lab2: nel primo un'istanza ha risposto con un errore alle prime due
  richieste e un'altra alla prima, 28 minuti dopo il riavvio, poi sono ripartite da sole; nel secondo, con le richieste
  fatte subito, nessuna delle cinque ha sbagliato (è la voce «`reset()` del pool» di «Non verificato», misurata su due
  casi soli; vale per ogni riavvio del cluster, anche quello della regolazione). Ruolo e database hanno il
  nome di AD-002 (`<workspace>_<istanza>`, senza CREATEDB, 32 connessioni); la riga è
  `hostssl <database>,postgres <ruolo> <indirizzo di A>/32 scram-sha-256` (`postgres` serve a Odoo per vedere se il
  suo database c'è, come nella riga locale). Mai una rete, mai `0.0.0.0/0`.
- **Il firewall di B non è ufw.** Docker inoltra una porta pubblicata prima che ufw la veda: misurato su lab2, con ufw
  attivo e «deny incoming» la porta di un container pubblicato rispondeva lo stesso. La regola sta quindi dove passa il
  traffico: catena `OMNIACLI-DB-FWD` agganciata a `DOCKER-USER` (verso la rete del cluster passa solo la sua porta dagli
  indirizzi delle concessioni, dalla rete del cluster escono solo le risposte) e `OMNIACLI-DB-IN` su `INPUT`; le
  riapplica a ogni avvio la unit del firewall dei workspace, da `/etc/omniacli/db-listen.json`.
- **La password non attraversa il collegamento.** Nasce su A; a B arriva il suo verificatore SCRAM-SHA-256, che
  PostgreSQL conserva così com'è. Un collegamento in HTTP semplice è rifiutato comunque: quello che risponde (porta e
  CA) decide dove si collega Odoo. Servono HTTPS o SSH.
- **L'indirizzo di A come lo vede B** è quello con cui A esce verso B (letto dal sistema, senza mandare pacchetti),
  proposto nel dialogo e modificabile (NAT). Il job lo verifica prima di creare qualcosa su A: raggiunge la porta come
  farà Odoo (TLS, certificato verificato contro CA e nome, ruolo e database); se `pg_hba` rifiuta dice l'indirizzo che
  B ha visto, se il firewall scarta dice che non risponde nessuno, e il database viene restituito.
- **Un nome già presente non si tocca.** Se sul cluster di B c'è già un ruolo o un database con quel nome (un'istanza
  di B, il database di un'altra macchina, quello che una rimozione ha lasciato), la concessione è rifiutata e non si
  riscrive niente: una seconda istanza con lo stesso nome prenderebbe ruolo e database della prima. A elimina su B solo
  quello che il suo job ha creato, e B esegue da un collegamento solo le richieste sui database di quel collegamento.
- **Rimozione**: A chiede a B di eliminare database e ruolo e togliere la riga, come `env_remove` per un'istanza di B;
  la regola del firewall resta finché un altro database usa quell'indirizzo. Se B non risponde la rimozione su A
  finisce lo stesso e il job dice cosa resta su B. La porta resta aperta, chiusa a tutti, finché l'admin di B non
  sceglie «Chiudi la porta…» (container rifatto, 0,4 s).
- **Su B** la scheda Database elenca «Da altre macchine»: database, indirizzo, collegamento, chi l'ha chiesto e
  quando, con Revoca, Consenti di nuovo, Elimina. Concessione, revoca ed eliminazione sono nel log degli accessi di B
  con il nome del collegamento e l'utente che ha chiesto su A.
- **Latenza, misurata il 22/11/2026 fra due macchine di prova** (Odoo 19 su lab1, cluster su lab2, ritardo aggiunto con
  `tc netem`): andata e ritorno di base 0,04 ms. Odoo paga circa **2,5 andate e ritorno per ogni query che conta**:
  la prima pagina dopo un riavvio (53 query) passa da 0,55 s a 0,65 / 0,78 / 1,20 s con 1 / 2 / 5 ms (130 andate e
  ritorno); il client web (9 query) da 6 ms a 23 / 38 / 81 / 153 ms con 1 / 2 / 5 / 10 ms (15); un elenco di 80 righe
  (4 query) da 5 ms a 106 ms con 10 ms (10). L'esempio di questo AD (una andata e ritorno per query) sottostimava. La
  **soglia dell'avviso è 1 ms**: una pagina di 200 query fa 500 andate e ritorno e aspetta mezzo secondo in più per
  la sola rete. Il dialogo mostra la misura (mediana di 7 connessioni TCP) prima di Crea, e il job la ripete sulla
  porta del database.
- **Limiti.** Su un'istanza così un backup non si ripristina sopra e non si verifica accanto (Odoo elimina e ricrea
  il database, e là il ruolo non ne crea): è rifiutato prima di toccare qualcosa, e si ripristina in un'istanza nuova.
  I dati stanno sullo storage del cluster di B, senza il tetto del disco di un workspace. Se A sta dietro un NAT
  l'indirizzo va scritto a mano. Un collegamento il cui URL passa da un proxy su un'altra macchina dà a B un nome che
  non è il suo. Il certificato del server dura 10 anni e non si rinnova da solo. Non costruiti: lo spostamento di
  un'istanza esistente su un cluster collegato, la staging, il ripristino a un minuto (AD-006).

Provato su lab1 e lab2: istanza creata in 1 min 41 s (3-4 s la parte di B; 90 s sono il controllo del disco del
workspace, che c'era già), Odoo risponde, `pg_stat_ssl` su
lab2 mostra TLS 1.3 da 172.30.0.11, rifiutati un altro database, un altro ruolo, la CA sbagliata, la connessione senza
TLS, un altro indirizzo (scartato dal firewall); la password si trova solo nel `.env` (600, root) di lab1; backup
riuscito; revoca e riammissione da lab2; rimozione da lab1 con lab2 che risponde e con il pannello di lab2 fermo.

**AD-004, costruito il 21/11/2026 per il database di un'altra macchina** (release 2026.1.5465): chi gestisce il
database può dare l'indirizzo della sua replica di lettura; OmniaCLI lo controlla, lo scrive in `odoo.conf`
(`db_replica_host`, `db_replica_port`, Odoo dalla 18), lo conserva alle riscritture e lo lascia passare nel firewall.
Provato con una replica in streaming vera: la replica vede la connessione TLS di Odoo. Se il primario cade Odoo non
passa alla replica da solo, e il campo lo dice.
**AD-004, opzione A, costruita il 22/11/2026: la replica di lettura del cluster della macchina, fatta e gestita
da OmniaCLI** (`managedreplica.py`, `workspace-helper machine-db-replica-*`; contratto UX-010). Due macchine: **P** ha
il cluster, **R** è un altro OmniaCLI con un collegamento (HTTPS o SSH) verso P e ospita la replica. Tutto si chiede
da R attraverso il collegamento; P non chiama mai R: quello che P ha da dire a R (eliminata, sostituita) R lo legge
al giro successivo (ogni 20 s). Provato su lab2 (P) e lab1 (R).
- **Su P**: un ruolo solo per la replica (`omniacli.replica`, REPLICATION, la password nasce su R e a P arriva il suo
  verificatore SCRAM), uno slot fisico riservato subito, `hostssl replication <ruolo> <indirizzo di R>/32
  scram-sha-256`, quell'indirizzo nel firewall del cluster, e un tetto a quanto lo slot trattiene
  (`max_slot_wal_keep_size`, 20 GB, regolabile: senza riavvio). La porta TLS si apre come in AD-003 se è chiusa (il
  container si rifà: detto nel dialogo, sul pulsante e chiesto due volte). Una replica per cluster.
- **Su R**: un progetto suo accanto a quello del cluster (`<storage>/.machine-db/replica17`), `pg_basebackup -Fp -X
  stream` attraverso lo slot, in TLS `verify-full` contro la CA di P; un hot standby in un container irrigidito
  (`read_only`, `cap_drop: ALL`, la slice del cluster), con `archive_mode=off` sulla riga di comando (non archivia
  mai, nemmeno promosso), in ascolto in TLS su una porta libera con una CA e un certificato suoi; `pg_hba.conf` lascia
  entrare solo l'indirizzo di P, come il ruolo di ogni istanza di P verso il suo database; il firewall lascia entrare
  P e lascia uscire la replica solo verso la porta di P. **I tablespace** di P puntano a percorsi dei dischi dei suoi
  workspace: il container monta una cartella della replica (`roots/<n>`) al posto di ogni storage di P, come nel
  cluster temporaneo di AD-006; la cartella di un tablespace nato dopo la crea il giro di R, dai percorsi che P dice.
  Il superuser della copia ha la password di P, che R non conosce: root entra dal socket, che è un tmpfs del
  container.
- **Leggere dalla replica** è una scelta per istanza di P (admin della macchina, Odoo dalla 18): `db_replica_host` e
  `db_replica_port` in `odoo.conf`, mai vuoti, più `db_sslmode = verify-full`; la CA di R arriva al container di Odoo
  come `PGSSLROOTCERT` con un `compose.override.yaml` (il `compose.yaml` dell'istanza non cambia di un byte, AD-007).
  Il primario resta il socket Unix, dove libpq non usa TLS: provato, Odoo parte e scrive con `verify-full`.
- **Misure (22/11/2026, lab1 e lab2, PostgreSQL 17.12, Odoo 19):**
  - creazione: cluster di 164 MB, job 8,4 s (P 2 s, copia di 181 MB in 3,9 s, avvio 1,6 s); cluster di 2,6 GB,
    copia di 2.622 MB in 5,2-6,6 s, job 9,6-11 s. Durante la copia le due righe dicono «copia in corso» con i byte;
  - in pari: 0 s; con il replay della replica in pausa, 25 s dopo la riga dice «in ritardo di 25 s»;
  - replica ferma: la riga chiusa di P dice «non collegata» 1,2 s dopo; con 200 MB scritti la riga dice «tiene
    196 MB», lo slot 205.687.016 byte (rapporto 1,00); una seconda volta 302 MB contro 299 MB;
  - tetto: portato a 0,1 GB con 208 MB trattenuti, al checkpoint lo slot è `lost` e la riga dice che va rifatta;
  - eliminazione da P: 23 s con un'istanza che legge (12 s il suo riavvio), slot e ruolo spariti, R cancella la sua
    copia al giro dopo (entro 45 s); da R: 16 s. Con il pannello di P fermo l'eliminazione da R finisce e dice cosa
    resta là; con il pannello di R fermo quella da P finisce, la copia resta e R la cancella entro 45 s dal suo avvio;
  - lettura accesa o spenta: job di 12 s; sulla replica `pg_stat_ssl` mostra TLS 1.3 da 172.30.0.12 con il ruolo
    dell'istanza; `/web/login` 200; una scrittura riesce;
  - **una lettura subito dopo un salvataggio va alla replica** (la voce di «Non verificato» di UX-010): con il replay
    in pausa, `read`, `web_read` e `web_search_read` della stessa sessione restituiscono il valore vecchio per almeno
    3 s dopo un `write` riuscito; con la replica al passo, il valore nuovo subito;
  - scheda Database: la route risponde in 0,6-0,9 s, la pagina in 1,4-2,1 s;
  - avviso del monitor: registrato dopo 10 minuti senza collegamento (su lab2 nessun destinatario configurato:
    nessuna mail vista partire);
  - **promozione con P che risponde**: 47,5 s in tutto; arresto di P 0,3 s, promozione 0,4 s; la prima produzione
    risponde 8 s dopo l'arresto, l'ultima delle cinque istanze dopo 47 s (una dopo l'altra, circa 10 s ciascuna);
  - **promozione con il pannello di P fermo**: 0,8 s; un'istanza di P ha scritto sul cluster vecchio dopo la
    promozione (la finestra detta nel dialogo: quella scrittura è persa); riavviato il pannello di P, R ha fermato il
    suo cluster 4 s dopo e puntato le cinque istanze in 48 s.
- **La promozione** ferma prima P (se risponde e non si ferma, niente viene promosso), poi `pg_promote` quando tutto
  il ricevuto è applicato, poi ogni istanza di P diventa un'istanza su un database di un'altra macchina (il gemello
  `-remote` del suo template: TLS `verify-full`, ruolo, password e database di prima; quello che era resta accanto,
  in `.before-promotion`). D3 come scelta in UX-010: il cluster di P riparte come sempre, e dopo una promozione fatta
  mentre P non rispondeva R riprova ogni 10 s. **I database che P ha concesso ad altre macchine** sono nella copia,
  ma le loro istanze continuano a cercarli su P e restano senza database: il dialogo li nomina, niente li ripunta
  (su lab1 `acme-linked` ha smesso di rispondere, come detto).
- **Non costruito**: la replica di un database dedicato; più di una replica per cluster, e più di una ospitata per
  macchina; le letture per le istanze di altre macchine (le concessioni); P ricostruito come replica dopo una
  promozione; la regolazione della replica (ha i valori di P: se R ha meno memoria di quanta P ne ha assegnata, la
  replica non parte e il job lo dice); il rinnovo del certificato della replica (10 anni). Se P sta dietro un NAT
  l'indirizzo con cui R lo vede arrivare non si può correggere dal dialogo. Uno storage aggiunto a P dopo la creazione
  non è montato nella replica: va rifatta.
- **Non verificato**: su quale ramo di Odoo gira il template `odooplm-20` (l'immagine non è sulle macchine di
  prova): il pulsante segue la versione dichiarata dal template (20, quindi acceso). Odoo 17: nessun template 17 in
  OmniaCLI, il rifiuto è coperto solo dai test. Il comportamento con decine di GB e su una rete vera (la copia qui
  viaggia a 400-500 MB/s fra due container della stessa macchina). La mappa ha il nodo della replica dentro il riquadro di R e la parola di stato
  sotto il cluster di P solo nello schema a più macchine (S1), più la voce `replica` in «Da guardare»: la linea
  puntinata fra il cluster di P e il nodo di R non è disegnata, e lo schema di una macchina sola (S2) non mostra la
  replica.
**Non costruita:** la replica creata e gestita da OmniaCLI per un database dedicato. Codice letto alla revisione
57a005c (`carlo_dev_2026`).
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

**Stato.** Costruito il 22/11/2026 (in testa, «AD-006, costruito»), con una differenza dalla decisione: la verifica
della domenica **non** gira nel cluster temporaneo, resta sul cluster condiviso com'era.

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
- ~~Che `pg_basebackup -Ft` con i tablespace si ripristini nel cluster temporaneo con `tablespace_map`~~: provato il 22/11/2026 su lab2 (AD-006 in testa): `tablespace_map` resta com'è e il container temporaneo monta una sua cartella al posto dello storage.
- Su quale ramo di Odoo gira il template odooplm-20, e quindi se ha la replica: non letto.
- Il supporto di `host=a,b` in `db_host` più `PGTARGETSESSIONATTRS` per seguire una replica promossa: possibile in libpq, non provato con Odoo.
- Le formule di regolazione e la quota di RAM di default sono indicazioni: nessuna misura di carico.
- Osservazione su ber, fuori dal mandato: c'è una sola base del ripristino a un minuto (20/11 alle 10:40) e nessuna del 21/11. Non ho controllato se il giro notturno sia girato.
- Su ber ho fatto solo letture via SSH (versioni, dimensioni, archivio); su nessuna macchina ho scritto niente.
