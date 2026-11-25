# AD-016: Le destinazioni dei backup: una per istanza, una predefinita per server, e il centrale che sa dove sono

Stato: **deciso** (risposte di Carlo del 25/11/2026 in §10). Risponde alle domande di UX-016 §12 (seconda versione, le scelte di Carlo del 23/11/2026). È la condizione che UX-016 pone prima della build.

Il codice è stato letto alla revisione `d6e8fee` (`carlo_dev_2026`). Le misure di questo documento vengono dal codice. Dove un numero è un'ipotesi lo dice, e i numeri delle macchine vere arrivano con la fetta 0.

Le regole di AD-017 e AD-018 valgono e qui non si ripetono. Le più toccate sono tre:
- la macchina non chiama mai il centrale;
- un dato del fornitore non compare dalla parte del cliente (`customerview`);
- REST `/api/v1` e MCP vanno sempre insieme.

Origine: Carlo, 23/11/2026, sul dialogo «Carica un backup» (5502). «i backup dovrebbero essere su una cartella unica… se dovesse andare giù il server i backup restano», poi «va in base ad ogni istanza/server, perché ogni volta che faccio il backup deve andare da qualche parte». Risposte del 23/11: D1-D7, D-N1…D-N5 di UX-016 §20. Non ci sono `TS-NNN` né `T<n>`/`HT<n>`.

## 0. Il problema e il costo, in numeri

**Com'è oggi.** Una destinazione è di una macchina o di un workspace e sta in `destinations.yaml` della macchina (600). Per ogni destinazione il file tiene impostazioni, segreti e la chiave `opk1-…`. Ogni copia è cifrata qui (`pullcrypto.encrypt_file`, AES-256-GCM a blocchi, l'ultimo marcato) e va in `<path>/<server>/<role>/<backup>.ocpf`. La scelta della destinazione sta nel **passo di backup della pianificazione** (`BackupStep.destination`, `schedules.yaml`), non nell'istanza.

| Cosa | Quanto | Fonte |
|---|---|---|
| Dove sta la scelta della destinazione | nel passo di backup di `backup.<server>.<role>`: spegnere i backup automatici toglie la scelta (`backup_schedule_save`, `data.pop("backup")`) | `app.py` ≈ 15232 |
| Letture di `BackupStep.destination` | 17, in 3 file (pianificazione, scheda Backup, mappa, «Copie in…», pagina Destinazioni) | `grep` |
| Punti che fanno un backup nella cartella di un'istanza | 8 chiamate di `backup_into` e 6 di `backup_before_update`, in 6 file. **Solo 1** (la pianificazione) copia fuori sede | `grep` |
| Dove gira la copia di oggi | dentro il job della pianificazione, che tiene il lock dell'istanza: 6 GB a 20 MB/s sono ≈ 5 min di istanza occupata dopo il backup, e il Git pull della stessa pianificazione aspetta | `schedules.py` 560-573, `jobs.py` 270 |
| Spazio della copia | una copia cifrata temporanea accanto al backup: per qualche minuto il disco dei backup tiene il file due volte | `destinations.upload` |
| Il nome della macchina nella copia | **assente**: due macchine con un server dello stesso nome e la stessa cartella si scrivono e si ruotano a vicenda | `instance_folder` |
| Chi sa leggere le copie se la macchina muore | nessuno, salvo chi ha salvato la chiave (`destination_key`) e le credenziali | `destinations.py` |
| rclone | non è fra le dipendenze del pacchetto (`control.in`): lo installa `provision.sh` | `packaging/` |
| Notte tipica | **ipotesi** UX-016: 12 produzioni, ≈ 16 GB a notte | UX-016 §20 |
| Lavoro stimato | ≈ 20 giornate in 9 fette, ognuna rilasciabile da sola (§9) | stima |

---

## AD-016: La forma

**Contesto.**
- D1: una predefinita per server (la macchina) e una scelta per istanza.
- D-N2: ogni backup di una produzione va fuori sede.
- D-N5: il centrale tiene, sigillate, le destinazioni di ogni macchina collegata.
- AD-017 mette dati, API, helper e backup sulla macchina, e pagine e liste nel centrale.
- AD-018 tiene i byte del cliente fuori dal VPS (18.7.1) e ha già il modello di «export interno solo per il collegamento» (`/api/identity/v1/export`).

**Opzioni considerate** (le scartate, con il motivo, sono in §11):
- A. **La macchina resta la sola che scrive e copia.** La scelta passa dalla pianificazione all'istanza (`destinations.yaml`). Ogni backup di una produzione entra in una coda di copia della macchina, fuori dal lock dell'istanza. Il centrale legge per collegamento un'istantanea delle destinazioni, chiavi comprese, e la usa solo quando la macchina non risponde: per elencare le copie e per passarle, il tempo di un job, alla macchina che ripristina.
- B. Il centrale fa le copie (le macchine gli mandano i backup).
- C. Un archivio unico per tutte le macchine (la prima versione di UX-016, scartata da Carlo).
- D. Il centrale che scarica, decifra e ricarica i byte di un ripristino.

**Decisione.** A, in nove parti (16.1-16.9).

**Motivazione.**
- 少: nessun meccanismo nuovo per i byte. Riusa `destinations.upload/rotate/fetch`, il `.ocpf` e `.omniacli-backups.json`. L'export interno è quello di AD-018 fetta 1, e le credenziali nel corpo di una richiesta sono quelle che la consegna già fa (`app.py` ≈ 26576).
- 美: la macchina che fa il backup è quella che lo copia, come oggi. Il centrale fa quello che già fa per le identità.
- 軽: la copia non tiene l'istanza, il centrale spento non ferma nessuna copia (AD-017 17.4), e i byte non passano dal VPS (AD-018 18.7.1).

**Contratto d'invarianti** (quello che il codice di oggi presume e non controlla):

| Invariante | Vale? |
|---|---|
| **Unicità**: `<path>/<server>/<role>/<file>` è di una sola macchina | **No**. I nomi dei server sono unici per macchina, non fra macchine: AD-018 ha trovato W con lo stesso nome su lab3 e lab4. La rotazione di una macchina può cancellare le copie dell'altra. Vincolo 8.4: ogni copia porta l'id della macchina (cifrato), e si ruota solo una copia di questa macchina |
| **Completezza**: un file col nome finale è una copia intera | **Non sempre**. S3 pubblica solo a caricamento finito. Su SFTP/FTP un rclone vecchio (1.53 di Ubuntu 22.04) scrive in place, e un'interruzione lascia un pezzo col nome giusto. La decifratura lo scopre (ultimo blocco marcato), l'elenco no. Vincolo 8.3: la copia è presente solo con i suoi fatti, scritti per ultimi |
| **Ordinamento**: le copie arrivano nell'ordine dei backup | **No**: la coda può copiare un backup delle 15 dopo uno delle 16. La rotazione legge la data dal nome (`STAMP`), non dal momento della copia: vale |
| **Reversibilità**: una copia fatta resta | **No**. Il fornitore o il cliente (la sua Dropbox) cancella quello che vuole. L'elenco della destinazione è la verità sulla presenza, la ricevuta dice solo tentativi ed errori |
| **Proprietà del dato**: `destinations.yaml` della macchina è la fonte | **Sì**, e resta così: l'istantanea del centrale è una copia, mai riscritta sulla macchina, mai letta dalla macchina |
| **Cardinalità**: le copie di un'istanza sono limitate | **Sì** per automatici (7+4+12), prima di un aggiornamento (3) e caricati (30 giorni). **No** per i backup a mano: oggi non ruotano nemmeno sulla macchina. Va deciso (§10.2) |
| La chiave di una destinazione non cambia | **Per nome sì, nel tempo no**: una destinazione cancellata e rifatta con lo stesso nome ha un'altra chiave, e le copie di prima non si leggono più. Il centrale tiene le chiavi superate (16.4) |

**Tracciabilità.** UX-016 §12 (domande 1-5) e §20 (D1-D7, D-N1…D-N5); AD-017 17.2, 17.4, 8.4; AD-018 18.6.5, 18.7.1, fetta 1.

### 16.1: Dove sta la scelta: la predefinita del server e quella dell'istanza

**Decisione.** In `destinations.yaml`, accanto alle destinazioni:
- `default`: il nome di una destinazione **della macchina** (mai di un workspace), oppure niente. Porta `since`, il momento della scelta.
- `instances`: `{<server>/<role>: {destination, since}}`, solo per le istanze che **non** seguono la predefinita. Un'istanza assente segue la predefinita (D5): cambiare la predefinita sposta lei e non le altre (UX-016 §11).

**La destinazione effettiva** di un'istanza la dà una funzione sola della macchina, che usano tutti (coda, scheda, mappa, campanella, pagina Backup):
1. staging o test: nessuna (D-N3). Una staging è un server che il file delle staging conosce, come per la mappa;
2. altrimenti la scelta dell'istanza, se la destinazione esiste e l'istanza può usarla (della macchina, o di un workspace di cui il server è parte, come `Scheduler.destination`);
3. altrimenti la predefinita;
4. altrimenti nessuna, e la produzione è «da guardare» (D-N1).

**La migrazione.** Al primo avvio la macchina porta ogni `BackupStep.destination` non vuoto in `instances`, con `since` = adesso. Fino alla fetta 8 più 14 giorni continua a scrivere nel passo di backup la destinazione effettiva: il rilascio di prima si comporta uguale (§9, si annulla).

**Chi sceglie.**
- La predefinita: un admin della macchina (P1, P1b), dalla pagina Destinazioni.
- Un'istanza: chi oggi cambia i suoi backup automatici (`backup_role`: admin per una produzione), fra le destinazioni di `destination_choices`.
- P2 vede «Predefinita del server» senza nome, e quelle del suo workspace.

**Scartate.**
- Lasciarla nel passo di backup: il backup a mano e quello prima di un aggiornamento non hanno una pianificazione, e spegnere gli automatici perderebbe la scelta.
- Un campo dell'istanza nel file dei server: le destinazioni e le loro scelte finirebbero in due file, e l'istantanea del centrale (16.4) dovrebbe leggerli tutti e due.
- Una predefinita del workspace: D-N4, no.

### 16.2: Ogni backup va fuori sede, e nessun aggiornamento lo aspetta (UX-016 §12.2)

**Decisione.** La copia non è più un passo del job che fa il backup. È un **job suo**, «Copia fuori sede di <file> in <destinazione>», sulla coda della macchina:
- **Un lock per macchina** (`_offsite`), non il lock dell'istanza. Una copia alla volta: 16 GB a 20 MB/s sono ≈ 14 minuti in fila, e la banda in uscita è una sola.
- **Chi entra in coda**: ogni file in una cartella di backup di una produzione, **fatto da questa macchina**, che non ha ancora la copia nella sua destinazione effettiva e che è nato dopo il `since` di quella destinazione. Così «dalla notte dopo» (D2) è una regola sola. «Fatto da questa macchina» lo dice la ricevuta che `backup_into` scrive (`backupcheck.record`); uno zip arrivato da un collegamento non ce l'ha. Entrano anche i caricati (16.8).
- **Chi la avvia**: un solo punto, non i 14 che fanno backup. Alla fine di ogni job che ha toccato una produzione (il momento `end` di `JobManager`, quello di `deploys.Recorder`), più un giro ogni 10 minuti e all'avvio, che recupera una copia persa a OmniaCLI fermo.
- **Il job che ha fatto il backup** finisce come prima. Nel log ha la riga «Copia fuori sede: job <id>» (e il link) oppure, senza destinazione, l'avviso «Nessuna copia fuori sede: …»: è il «⚠ con avvisi» di UX-016 §6.

**Il backup prima di un aggiornamento (la domanda 2): l'aggiornamento parte appena c'è il backup sulla macchina.** La copia va dopo, nel suo job. Esempio: `acme` da 6 GB, 4 aggiornamenti al mese, 5 minuti risparmiati per aggiornamento. Il prezzo è una finestra di ≈ 5 minuti in cui il backup di prima dell'aggiornamento è solo sulla macchina. Se la macchina muore lì, fuori sede c'è la copia della notte.

**Il backup automatico.** Anche lui passa dalla coda: il job della pianificazione non tiene più l'istanza durante la copia, e il suo Git pull non aspetta. Quello che UX-016 §6 chiede resta:
- la riga della pianificazione prende l'esito della sua copia, il peggiore dei due;
- la copia fallita manda «Se fallisce, avvisa» della pianificazione (il job della copia porta il suo `notify`);
- la voce entra nella campanella.

**La rotazione fuori sede** è quella della macchina, per tipo (D3), fatta dal job della copia dopo il caricamento:
- automatici: `rotation_kept` come oggi;
- prima di un aggiornamento: gli ultimi `KEEP_PRE_UPDATE` (3);
- caricati: 30 giorni;
- a mano e archivi: §10.2.

La rotazione gira anche nelle destinazioni di prima dell'istanza, finché la ricevuta le nomina e la destinazione esiste (UX-016 regola 7). Una macchina ferma non ruota niente (D3).

**Scartate.**
- La copia dentro il job che fa il backup: l'aggiornamento aspetta 5 minuti ogni volta, e un «Backup adesso» tiene l'istanza ferma per tutta la copia.
- Un aggancio nei 14 punti che fanno un backup: un 15° nuovo dimenticherebbe la copia.
- Copie in parallelo: stessa banda, nessun guadagno, e due cifrature temporanee sul disco.

### 16.3: La copia, i suoi fatti, la ricevuta

- **Il nome** resta `<path>/<server>/<role>/<file>.ocpf`, e per i caricati `<path>/_uploads/<workspace>/<file>.ocpf`. Niente nome della macchina in chiaro: la cartella si vede nella Dropbox del cliente.
- **I fatti** sono un file accanto, cifrato con la stessa chiave, con un suffisso che **non** finisce in `.ocpf` (l'elenco del rilascio di prima non lo deve contare come una copia). Contengono:
  - l'**id della macchina**: un id casuale stabile, nuovo, nel suo stato e nel purge, che non cambia con il nome;
  - server, ruolo, workspace, tipo, momento del backup, SHA-256 e dimensione;
  - versione di Odoo, database, moduli installati, allegati sì o no: quello che `dump_info` legge oggi dal dump.

  Si scrivono **dopo** la copia: una copia è presente solo con i suoi fatti. Una copia senza fatti è un caricamento interrotto, che la rotazione toglie. Fanno eccezione le copie nate prima di questo AD, riconosciute dalla data nel nome.
- **La ricevuta** sta in `.omniacli-backups.json`, accanto allo SHA-256: per ogni destinazione `tried`, `copied`, `error`, e il segno «fatto qui». Serve alla coda e ai messaggi, non alla presenza (contratto, reversibilità). Quando il file locale sparisce sparisce anche lei (`backupcheck.save`). Di una copia rimasta solo fuori sede si sa quello che dicono l'elenco (data della destinazione) e i fatti.
- **Spazio**: prima di cifrare, la coda controlla che il disco dei backup abbia la dimensione del file più 1 GB. Altrimenti la copia non parte, con il motivo, ed è «da guardare».

### 16.4: Cosa sa il centrale (domande 1 e 5 di UX-016 §12)

**Decisione.**
1. **Un export interno sulla macchina**, `GET /api/offsite/v1/snapshot`, come `/api/identity/v1/export`:
   - risponde solo al collegamento con ruolo admin (dopo `X-OmniaCLI-Role-Cap`), mai a una persona, a un token, a un ambito delegato;
   - `Cache-Control: no-store`, ogni lettura nel registro con `on_behalf`;
   - il proxy `/links/<m>/…` lo rifiuta con 404, come `api/identity/` e `api/pages/`.

   Contiene l'id della macchina e le destinazioni intere (impostazioni, segreti, chiave). Contiene la predefinita, le scelte delle istanze e l'inventario delle istanze (server, ruolo, workspace, produzione o staging, versione di Odoo). Non elenca le copie.
2. **Interruttore per collegamento, spento di default** («Tieni qui le destinazioni di <m>…», Collegamenti, conferma con il nome, REST e MCP insieme, nel registro). È il «per ber e ridix si richiede prima» di Carlo: si accende su lab1 e lab2 subito, sulle macchine dei clienti dopo il loro sì. Spento, il file sul centrale si cancella.
3. **Quando si rilegge.** `/api/info` dice in più `offsite_revision`, un hash di predefinita, scelte e destinazioni, senza segreti: un'aggiunta dentro `agent_api` 4 (AD-017 17.2), come `panel`. Il centrale lo legge già ogni 10 minuti e rilegge l'istantanea quando cambia, e subito dopo una scrittura riuscita che passa dal collegamento su destinazioni o scelte (come la rilettura di AD-018 fetta 2). Il ritardo massimo è quindi 10 minuti. Esempio: una destinazione creata sul pannello di lab1 alle 02:55, lab1 che muore alle 03:00, il centrale che non la conosce. Per quel caso c'è la rete: aggiungere la destinazione a mano con la sua chiave (oggi `destination_save` accetta la chiave data).
4. **Le chiavi che cambiano.** Il centrale non sostituisce: tiene ogni coppia (destinazione, chiave) che ha visto, con l'impronta e l'ultima volta che l'ha vista.
   - Una chiave superata di una destinazione **della macchina** resta **13 mesi**: la mensile più vecchia ne ha 12.
   - Una destinazione **di un workspace** che sparisce dall'istantanea (il cliente l'ha tolta, o il workspace è stato consegnato) si cancella subito, credenziali e chiave: è una revoca del cliente.
   - Una copia che non si decifra con la chiave di adesso si prova con le superate dello stesso posto (tipo, host o bucket, cartella), dalla più nuova.
5. **Dove e come.** `machine-destinations/<link>.json` (600, cartella 700), in un nuovo slot del backup cifrato del centrale (AD-017 17.4.3) e nel purge. Mai in un modello di pagina, in una risposta, in un registro. **«Sigillata» si legge così**: il file non si legge da nessuna API e viaggia solo dentro il backup cifrato. Non lo cifro sul centrale, per la ragione di AD-018 18.6.5: la chiave starebbe accanto, e accanto ci sono già i token admin di ogni macchina. La lettura più forte è §10.1.
6. **Le destinazioni del workspace (domanda 5).** Il centrale le conosce, nella stessa istantanea e con lo stesso interruttore: il ripristino a macchina ferma non chiede la chiave al cliente. Se il cliente non lo accetta: §10.3.

**Scartate.**
- La macchina che spinge le sue destinazioni al centrale: chiamerebbe il centrale.
- Leggere `destinations.yaml` con la shell del collegamento: nessun registro, nessun limite.
- L'istantanea tenuta sulle altre macchine (lab2 che sa di lab1): i segreti di tutti su tutte.

### 16.5: La pagina Backup e le copie fuori sede

- **Macchina che risponde.** `/api/backups` aggiunge `places` a ogni riga (UX-016 §11).
  - La macchina legge ogni destinazione usata **una volta**, con un elenco ricorsivo. Lo tiene 10 minuti in memoria e lo rinnova in background: una richiesta non aspetta mai rclone.
  - Senza elenco pronto, la pastiglia è «…»; una destinazione che non risponde entro 5 s è la sua riga «non risponde».
  - Le copie della destinazione senza un file locale diventano righe con «Macchina» assente e «Riporta qui» (16.6).
  - Si considerano solo le cartelle delle istanze di questa macchina, e la rotazione legge i fatti solo dei candidati alla cancellazione, mai di tutte le copie.
- **P2**: i nomi delle destinazioni del server diventano «Fuori sede», senza percorso. Lo fa la macchina (`customerview`), non il centrale.
- **Macchina che non risponde** (lista del centrale scaduta, 5 s), con l'interruttore acceso:
  - il centrale elenca lui le destinazioni dell'istantanea, una sola volta per destinazione, in parallelo, 5 s ciascuna;
  - prende le cartelle `<server>/<role>` dell'inventario di quella macchina e le copie che hanno i fatti;
  - ne fa righe «Macchina ?» con «Scarica» e «Ripristina in un'istanza nuova…», niente Elimina.

  **Nessuna cache sul centrale** (AD-017 8.4): ogni apertura rilegge, e le righe della macchina ferma arrivano dopo quelle delle altre. I fatti si leggono solo quando servono (piano, scarico) e lì si controlla che l'id della macchina sia quello dell'istantanea. Solo P1 vede queste righe.
- **Scarica a macchina ferma**: il centrale legge la copia con rclone e la decifra **mentre la manda**, blocco per blocco, senza nessun file sul suo disco. Se l'ultimo blocco non si apre la connessione si chiude a metà e il browser dà un download fallito, mai un file «completo». È il solo caso in cui dei byte passano dal centrale: li chiede una persona per sé, non è un ripristino.

### 16.6: Il ripristino su un'altra macchina (domanda 3 di UX-016 §12)

**Decisione: la macchina che riceve legge la destinazione, con le impostazioni e la chiave che il centrale le dà per quel job.**
1. **Il piano** (P1, dialogo di UX-016 §8). Il centrale apre i fatti della copia (pochi KB) e li manda alla macchina scelta, dentro una richiesta interna solo per il collegamento admin. La macchina fa il piano di oggi (`restore_new`, prova a vuoto) **dai fatti**: versione, moduli, spazio, che ora conta anche la copia cifrata temporanea. Zero byte nell'area di import, in < 3 s.
2. **Crea.** La stessa richiesta senza prova a vuoto porta nel **corpo** la destinazione intera (come i segreti della consegna, `app.py` ≈ 26576).
   - La macchina la tiene **solo nella memoria del job**: mai in `destinations.yaml`, mai nel log, a rclone come oggi nell'ambiente di un comando.
   - Il primo passo «Porta il backup da <destinazione>» scarica nell'area di import del workspace scelto su quella macchina, decifra e controlla lo SHA-256 contro i fatti; poi i passi di oggi.
   - Nel registro: sul centrale chi l'ha chiesto e da quale macchina viene la copia; sulla macchina che riceve il collegamento, `on_behalf`, la destinazione e il file.
3. **Ripresa**: dentro il job, con i tentativi di rclone. Se la macchina che riceve si riavvia, il job fallisce con «Riprova dal centrale»: la credenziale non c'è più, ed è voluto.
4. **Macchina della copia che risponde**: stesso percorso, dalla destinazione e mai da macchina a macchina. Un backup che è solo sulla macchina (nessuna destinazione) non si ripristina altrove: l'opzione è spenta con il motivo.
5. **Stessa macchina** («Riporta qui», o il ripristino di un backup uscito dalla rotazione locale): `destinations.fetch` di oggi, dalla destinazione della ricevuta o dell'elenco, non da quella della pianificazione (`remote_of`).
6. **Il workspace** sulla macchina che riceve lo sceglie P1. L'istanza nuova è di quel workspace, come oggi.

**Scartate.**
- Il centrale che scarica, decifra e carica a pezzi (la route di 5502): 6 GB passano due volte dal VPS, contro AD-018 18.7.1, e il VPS diventa un nastro.
- Copiare la destinazione di lab1 in `destinations.yaml` di lab2: le credenziali di un cliente restano su una macchina che non è sua.

### 16.7: La prima domenica del mese dalla copia (domanda 4 di UX-016 §12)

- La prova settimanale di oggi (`verify_weekday`, `backupcheck.verify`) la prima domenica del mese diventa la prova **dalla copia** (D6): un job sull'istanza, dopo che la copia di quella notte è riuscita.
- **Dove**: sulla stessa macchina, in una cartella 700 sul disco dei backup. Si scarica la copia di stanotte, si decifra (si tolgono i byte cifrati appena decifrati), si confronta lo SHA-256 con la ricevuta del file locale, poi `verify_backup` come oggi, e si cancella.
- **Spazio**: copia cifrata più decifrata, poi lo spazio di `verify_backup`. Se non basta, la prova si fa sul file locale come le altre domeniche, il job finisce «⚠ con avvisi» e la produzione è da guardare.
- **Errore di decifratura o SHA-256 diverso**: il giro **fallisce come oggi**, con «Se fallisce, avvisa». È proprio il caso che questa prova deve trovare.
- **L'esito** va nella ricevuta (`verified`, con `from: <destinazione>`). Il `title` della pastiglia lo dice finché il file locale c'è.

### 16.8: Carica un backup

- Il file caricato resta **30 giorni** (D4, oggi `IMPORT_KEEP_DAYS` = 7) e va nella **predefinita** della macchina, in `_uploads/<workspace>/`, con la stessa coda. Senza predefinita resta solo qui, e non è da guardare (non è il backup di una produzione).
- Fuori sede scade a 30 giorni dalla data nel nome. Elimina toglie le due copie (UX-016 §9.5), e fuori sede solo una copia con i fatti di questa macchina.
- 50 GB al massimo vogliono 50 GB in più per la cifratura temporanea: lo dice il controllo di spazio di 16.3. Il caricamento non si blocca mai per la copia.

### 16.9: Rispetto ad AD-017 e AD-018

- **Il contratto di versione.** Tutto quello che la macchina dice in più è un'aggiunta dentro `agent_api` 4: `places`, i campi della pagina Destinazioni e della scheda Backup, `offsite_revision`. **Niente numero nuovo.** Alzarlo farebbe tornare alle loro pagine, con la riga «aggiornala», le macchine a 3. I template del centrale mostrano un campo solo se il modello ce l'ha, come «Scarica» (AD-017 17.2). Le route nuove sono interne, come `/api/pages/*`, e una macchina vecchia risponde 404: per il centrale «la sua OmniaCLI è più vecchia», come `MAP_OLDER`.
- **I modelli delle pagine.** Destinazioni (classe B) e istanza (classe C) restano senza segreti, provato per valore (`test_the_class_b_models_carry_no_secret_value`); l'export di 16.4 non è un modello.
- **Modalità `agent`.** Coda, copie, rotazione e prova della domenica sono della macchina: girano col centrale spento (AD-017 17.4). Il centrale serve solo quando una macchina è ferma.
- **AD-018.** Le persone dei clienti vedono `places` già redatto dalla macchina, attraverso il centrale casa. Un workspace passato gestisce le sue destinazioni dalle pagine scritte dal centrale, che le salva sulla macchina. La consegna porta con sé le destinazioni del workspace e le scelte delle sue istanze, non la predefinita della macchina che consegna.
- **Spostamento (AD-014).** Un server spostato fa i backup sull'host. Se segue la predefinita, sull'host segue quella dell'host; una scelta di una destinazione del workspace viaggia con la `sync` di oggi. Le copie fatte dalla casa restano dove sono (regola 7).

---

## 8. Vincoli per il dev-lead

1. **Una sola funzione** dà la destinazione effettiva di un'istanza (16.1) e tutte la usano: coda, scheda, mappa, campanella, `/api/backups`, istantanea. Dopo la fetta 2, `grep` non trova più letture di `BackupStep.destination` fuori dalla migrazione.
2. **La copia non prende mai il lock di un'istanza.** Prova: un aggiornamento con backup prima parte e finisce mentre la copia del suo backup gira (UX-016 §18 criterio 3). Il tempo dell'aggiornamento si misura con e senza copia, al più +10%.
3. **Una copia è presente solo con i suoi fatti**, scritti dopo. Il suffisso dei fatti non finisce in `.ocpf`.
4. **La rotazione cancella solo copie con i fatti di questa macchina** (o nate prima di questo AD). Prova: due macchine con lo stesso server e la stessa destinazione, 0 copie dell'altra cancellate.
5. **L'elenco è la verità sulla presenza**, la ricevuta no. Elenco di una destinazione: una volta per destinazione, 10 minuti in memoria sulla macchina, mai atteso da una richiesta oltre 5 s. Nessuna cache sul centrale (AD-017 8.4). I fatti si leggono solo per piano, scarico e rotazione.
6. **Segreti.** Le credenziali di una destinazione stanno solo in `destinations.yaml` della macchina, in `machine-destinations/` del centrale e, il tempo di un job, nella memoria della macchina che ripristina. Un test cerca **per valore** password, token e chiavi di destinazione nei modelli, nelle risposte, nei log dei job e nei file della macchina che ripristina: 0. L'export di 16.4 e le richieste interne di 16.6 rispondono solo al collegamento admin, il proxy le rifiuta con 404, e ogni lettura e uso è nel registro con `on_behalf`.
7. **Mai un rifiuto per la copia**: un backup, un aggiornamento o un caricamento non si fermano mai per una destinazione mancante, piena o ferma (D-N1). La mancanza è un avviso nel job e una voce della campanella, mai una mail ogni notte.
8. **Riservatezza di P2**: nomi di macchine, del centrale, delle destinazioni del server e percorsi mai nel DOM o nel JSON (UX-016 §18 criterio 11), redatti sulla macchina.
9. **REST e MCP insieme** per: predefinita (scegli, togli), destinazione di un'istanza, lista con `places` (`list_backups`), interruttore per collegamento (16.4), ripristino da una copia su un altro server (sul centrale). Le route interne (export, piano e ripristino da destinazione) non hanno gemelli, come `/api/pages/*`.
10. **Registro (NIS 2)**, le voci di UX-016 §11, più ogni lettura dell'istantanea e ogni ripristino con credenziali passate.
11. **rclone** sta in `Recommends` del pacchetto. La pagina Destinazioni del centrale dice se manca: il centrale lo usa già per il suo backup (AD-017 fetta 7).
12. Purge: l'id della macchina e `machine-destinations/`. Niente file nuovi in `/etc`.

## 9. Piano a fette (ognuna si rilascia da sola e si annulla)

Il lab è quello di UX-016 §18:
- il centrale è il pannello locale dal worktree;
- le macchine sono lab3 e lab4;
- due SFTP di prova sulla rete `omnia-lab`: `sftp-a`, predefinita di lab3, e `sftp-ws`, del workspace `acme` di lab3;
- lab4 senza predefinita.

Mai su ber, ridix, inaria, odoo-test. lab1 e lab2 dopo il rilascio, per l'aspetto finale.

0. **Misura**, in sola lettura, su lab1, lab2 e il centrale di sviluppo, con il sì di Carlo per ogni macchina. Per ognuna: produzioni, destinazioni, chi ha una destinazione oggi, la dimensione dell'ultimo backup di ogni produzione (la notte vera contro i 16 GB dell'ipotesi), la velocità delle copie dai log dei job (le righe `--stats` di rclone), la versione di rclone (scrittura in place o no), server con lo stesso nome fra macchine.
   - *Stima*: 0,5 g.
1. **La scelta**: predefinita e istanza in `destinations.yaml`, la migrazione, la funzione della destinazione effettiva. La pagina Destinazioni con «Predefinita», «Rendi predefinita…» con la chiave mostrata prima, «Non è più predefinita…» e la `note-box`. «Copia fuori sede in» fuori da «Backup automatici». Le copie restano quelle di oggi (solo automatici), verso la destinazione effettiva.
   - *Prova*: UX-016 §18 criteri 1 e 2. Una produzione con «Copia anche in» di prima tiene la sua destinazione dopo la migrazione.
   - *Si annulla*: con il rilascio di prima, che legge `BackupStep.destination` ancora scritto (16.1).
   - *Stima*: 2 g.
2. **La coda**: ogni backup di una produzione, job suo sotto `_offsite`, avvio alla fine dei job e ogni 10 minuti, ricevute, controllo dello spazio, rotazione per tipo, avvisi nel job, i due tipi della campanella e della mappa. L'esito della copia nella riga della pianificazione.
   - *Prova*: criteri 3, 4 e 5. Il backup prima di un aggiornamento: l'aggiornamento finito prima della copia, misurato. OmniaCLI fermato a metà di una copia: al riavvio la copia riparte dal giro.
   - *Si annulla*: con il rilascio di prima. Le chiavi in più delle ricevute il codice di prima non le legge.
   - *Stima*: 3,5 g.
3. **Fatti e colonna Dove**: i fatti accanto alla copia, l'id della macchina, la rotazione che li rispetta, `places` in `/api/backups`, la colonna Dove, il filtro della testa, Elimina con la casella. Nella scheda Backup la lista con «Riporta qui» al posto di «Copie in…».
   - *Prova*: criteri 6, 10, 11 e 13 (500 backup, prime righe < 2 s). Due macchine con lo stesso server sulla stessa cartella: 0 cancellazioni sbagliate.
   - *Si annulla*: con il rilascio di prima, che non conta i fatti come copie (vincolo 3).
   - *Stima*: 3,5 g.
4. **Il centrale conosce le destinazioni**: l'export interno, `offsite_revision`, l'interruttore per collegamento, le chiavi superate, lo slot del backup cifrato.
   - *Prova*: acceso per lab3, una destinazione nuova su lab3 è sul centrale entro 10 minuti. Una destinazione rifatta con un'altra chiave tiene la vecchia, una del workspace tolta sparisce subito. Lo 0 segreti per valore del vincolo 6. `/links/lab3/api/offsite/…` dà 404. Spento, il file sparisce.
   - *Si annulla*: con l'interruttore, poi con il rilascio di prima.
   - *Stima*: 2,5 g. Si può fare in parallelo alle fette 2-3.
5. **Una macchina ferma**: righe del centrale dalle destinazioni, «Macchina ?», Scarica decifrato mentre arriva.
   - *Prova*: criterio 7 (`docker stop lab3`, entro 7 s, SHA-256 dello scaricato uguale ai fatti). Una copia accorciata a mano dà un download fallito. 0 file sul disco del centrale dopo lo scarico.
   - *Si annulla*: con il rilascio di prima del centrale.
   - *Stima*: 2,5 g.
6. **Il ripristino altrove**: il piano dai fatti, le credenziali nel corpo, il passo «Porta il backup da…».
   - *Prova*: criterio 8, con lab3 fermo e l'istanza su lab4 in funzione. Dopo il job, `destinations.yaml` di lab4 invariato byte per byte e 0 valori segreti di `sftp-a` nei file e nei log di lab4. Un riavvio di lab4 a metà: il job fallisce con «Riprova dal centrale».
   - *Si annulla*: con il rilascio di prima. Su lab4 resta solo l'istanza fatta.
   - *Stima*: 3 g.
7. **Caricamento**: 30 giorni, la copia nella predefinita, Elimina che toglie le due.
   - *Prova*: criterio 9.
   - *Si annulla*: con il rilascio di prima, che però cancella in locale i caricati con più di 7 giorni (le copie fuori sede restano): va detto nella nota di rilascio.
   - *Stima*: 1 g.
8. **La prima domenica**: la prova dalla copia, con il ripiego sul file locale per lo spazio.
   - *Prova*: criterio 12 con l'orologio di prova. Una copia con un byte cambiato: il giro fallisce e «Se fallisce, avvisa» parte una volta.
   - *Si annulla*: con il rilascio di prima (la prova sul file locale).
   - *Stima*: 1 g.

Totale ≈ 20 giornate (19,5). Le fette 1-3 (9 g) danno a Carlo «ogni backup va da qualche parte»; le 4-6 (8 g), «la macchina è morta, la rimetto su un'altra».

## 10. Da confermare con Carlo (la raccomandata per prima)

1. **Come è «sigillata» la copia delle destinazioni sul centrale.**
   - Raccomandata: **file 600 sul centrale, fuori da ogni API, dentro il backup cifrato** (16.4.5). Esempio: lab1 si ferma alle 03:00; alle 09:00 la pagina Backup mostra le sue 46 copie, e in 3 clic una è un'istanza su lab2, senza digitare niente. Chi ruba il disco del VPS ha le credenziali delle destinazioni, ma ha già i 7 token admin che leggono le macchine.
   - Alternativa: sigillata alla chiave pubblica del backup del centrale (AD-017 fetta 7), aperta con la passphrase da 30 caratteri solo quando una macchina è ferma. Protegge chi legge il file senza i token, che però stanno accanto. Al momento del guasto serve la passphrase: 1 campo in più, e senza di lei nessuna copia si vede.
2. **Quanto restano fuori sede i backup a mano e gli archivi** (oggi non ruotano nemmeno sulla macchina).
   - Raccomandata: **gli ultimi 5 per istanza**, come i 3 di prima di un aggiornamento. Esempio: `acme` da 6 GB, 2 backup a mano al mese: fuori sede 30 GB al massimo, invece di 144 GB dopo un anno.
   - Alternativa: restano finché la copia locale c'è, poi 30 giorni. Si cancellano da soli quelli che qualcuno ha tolto a mano, ma un backup a mano tenuto sulla macchina per un anno resta un anno anche fuori.
   - Alternativa: mai cancellati. Crescono senza limite (~144 GB/anno per `acme`).
3. **Le destinazioni del workspace del cliente nel centrale.**
   - Raccomandata: **sì, sotto lo stesso interruttore per collegamento** e con la cancellazione immediata quando il cliente le toglie (16.4.4). Esempio: `acme-shop` copia nella Dropbox di `acme` e lab3 muore: P1 la rimette su lab4 in 3 clic.
   - Alternativa: no. A macchina ferma serve che il cliente dia la chiave da 44 caratteri e il token della sua Dropbox, che si aggiungono a mano come destinazione: ore, se il suo tecnico non risponde.
4. **Il backup automatico che non aspetta la sua copia** (16.2).
   - Raccomandata: **sì**, e la riga della pianificazione prende l'esito della copia. Esempio: 12 produzioni alle 02:00, la copia di `acme` (6 GB) non tiene più l'istanza 5 minuti, e il Git pull della stessa pianificazione parte subito.
   - Alternativa: com'è oggi, la copia dentro il job della pianificazione. L'istanza resta occupata durante la copia, ma l'esito è un job solo.

### Le risposte di Carlo (25/11/2026)

1. **La copia delle destinazioni sul centrale:** file 600 fuori da ogni API, dentro il backup cifrato del centrale (la raccomandata).
2. **Backup a mano e archivi fuori sede:** **mai cancellati**, contro la raccomandata. La rotazione fuori sede non tocca i backup a mano né gli archivi: restano finché qualcuno non li elimina (Elimina con la casella «anche fuori sede», UX-016 §9.5). Il prezzo è scritto: ≈ 144 GB all'anno per un'istanza come `acme` (6 GB, 2 backup a mano al mese). Conseguenza per la build: la pagina Destinazioni e la campanella dicono quanto occupano i backup a mano fuori sede di ogni istanza, così chi paga la destinazione lo vede.
3. **Le destinazioni del workspace del cliente nel centrale:** sì, sotto lo stesso interruttore per collegamento, cancellate subito quando il cliente le toglie (la raccomandata).
4. **Il backup automatico che non aspetta la sua copia:** sì, la copia va nel suo job e la riga della pianificazione prende il suo esito (la raccomandata).

## 11. Alternative scartate

- **B. Il centrale fa le copie.** Ogni notte 16 GB passerebbero dal VPS, e senza centrale niente andrebbe fuori sede, contro AD-017 17.4 e AD-018 18.7.1.
- **C. Un archivio unico per tutte le macchine.** È la prima versione di UX-016, scartata da Carlo il 23/11 (D1).
- **D. Il centrale come canale dei byte di un ripristino.** Il motivo è in 16.6.
- **Il nome della macchina nella cartella della copia.** Si vedrebbe in chiaro nella Dropbox del cliente; l'id cifrato nei fatti dà la stessa unicità (contratto, unicità).
- **Cifrare in flusso verso `rclone rcat`** per non tenere la copia cifrata temporanea. Toglierebbe il doppio spazio, ma con rclone vecchi un caricamento interrotto e la sua ripresa cambiano per tipo di destinazione. Resta la copia temporanea di oggi con il controllo dello spazio; si riapre se la fetta 0 trova macchine col disco stretto.
- **Un `agent_api` 5.** Farebbe perdere le pagine del centrale alle macchine a 3, per campi che sono solo aggiunte.

## Non verificato

- I numeri veri di una notte (dimensioni, velocità): l'ipotesi è di UX-016, la misura è la fetta 0.
- La versione di rclone sulle macchine, e se scrive in place su SFTP e FTP: decide quanto conta il vincolo 3 per le copie di prima.
- Il tempo di un elenco ricorsivo con 500-1.200 oggetti (copie e fatti): atteso 1-3 s (UX-016 «Non verificato»). Se supera 5 s, la riga della destinazione dice «non risponde» fino alla lettura in background.
- Che `restore_new` possa fare la prova a vuoto dai soli fatti: oggi legge il dump (`dump_info`). La fetta 6 lo prova.
- `pullcrypto` non ha una decifratura in flusso: la fetta 5 la scrive sullo stesso formato. Il formato a blocchi lo permette, ed è da provare con un file accorciato.
- Il trattamento dei dati: i backup dei clienti in una destinazione dell'integratore e le credenziali delle loro destinazioni sul VPS (16.4) vanno controllati prima di accendere l'interruttore su ber e ridix (UX-016 «Non verificato», AD-018 18.6.7).
- Che nessuna macchina abbia oggi due server con lo stesso nome nella stessa cartella di una destinazione condivisa: fetta 0.

## Tracciabilità

UX-016 seconda versione (23/11/2026), §11-12 e §20, e le risposte di Carlo del 23/11. AD-017 (17.2 contratto, 17.4 centrale fermo, 17.4.3 backup cifrato, 8.4 niente cache), AD-018 (fetta 1 export interno, 18.6.5 segreti a riposo, 18.7.1 i dati non passano dal VPS), AD-014 (spostamento e destinazioni del workspace sull'host). Codice letto: `destinations.py`, `schedules.py` (`BackupStep`, `Scheduler.run`), `ops.py` (`backup_into`, `backup_before_update`), `jobs.py` (lock per istanza, momenti `begin`/`end`), `backupcheck.py`, `pullcrypto.py`, `centralbackup.py` (`INVENTORY`), `app.py` (`backup_schedule_save`, `remote_of`, `remote_backup_fetch`, `destination_save`, `backups_list`, `backup_of` della mappa, `restore_new`, `IMPORT_KEEP_DAYS`). Nessun `TS-NNN`, `T<n>`/`HT<n>`.
