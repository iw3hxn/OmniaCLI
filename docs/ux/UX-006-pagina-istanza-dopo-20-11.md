# UX-006 — La pagina dell'istanza dopo le funzioni del 20/11

Stato: contratto di design, prima della build. Implementa chi scrive `omniacli/templates/server.html` (Jinja + JS senza framework, classi esistenti `.tabs`, `.panel`, `.tiles`, `.primary`, `.small`, `details`). Le stringhe vanno in `omniacli/i18n/it/*.json`. Nessuna funzione nuova nel backend, salvo la decisione D3.
Valgono le regole comuni e i criteri di UX-001…005: questo contratto li applica alla pagina e non li ripete.
Origine: Carlo, 20/11/2026: «OmniaCLI la vedo ancora troppo poco maturo. Gli manca proprio l'usabilità». In un giorno sono state aggiunte circa dieci funzioni alla pagina senza contratto. Misurato con Playwright, 1440×900, IT, admin: 8 tab, 2 `.primary` visibili in 3 tab, 20 pulsanti senza `title`, date ISO, inglese in Monitoraggio, tab Database alto 1875 px.
Riferimento: UX-003/004 e il mockup `docs/ux/mockups/3-istanza.html`, che ha già i 5 tab qui sotto.

## 0. Cosa vede oggi l'utente (aggregato del 20/11)

| Tab | Cosa c'è | Difetto |
|---|---|---|
| Ambienti | testa con Versione, Raggiungibile, Worker, Database, Repository; tabella Ambienti; «Percorsi e comandi della shell»; ultima azione | il nome del database è già in testata; i repository compaiono 3 volte nella pagina; «dal 2026-11-20 10:39:51» |
| Backup | modulo dei backup automatici sempre aperto; elenco; «Indietro a un minuto (ripristino point-in-time)»; copie remote | il modulo spinge l'elenco sotto la piega; «WAL», «backup di base», «segmenti»; primario rosso «Nuova staging a questo minuto» |
| Log | log in diretta | va bene |
| Monitoraggio | 3 tile + 2 grafici | «Misurato su: the containers of /var/lib/…» |
| Database | 5 tile + 5 tabelle (1875 px) | nessun verdetto; «pg_stat_statements…» grezzo; nomi di tabelle e indici in faccia; date ISO |
| Addon | tabella dei repository con Aggiorna/Elimina; «Aggiungi un repository» sempre aperto | secondo elenco dei repository; «Aggiungi» primario |
| Configurazione di Odoo | Normali/Estesi; nome tecnico in grassetto, etichetta sotto | primario «Salva e riavvia»; il nome tecnico prima della parola dell'utente |
| Impostazioni | fatti dell'istanza; terzo elenco dei repository; Deploy al push | fatti tecnici in primo piano; l'aiuto del webhook è un paragrafo |

## 1. Persone e lavoro primario

| Persona | Chi è nel codice | Lavoro primario sulla pagina |
|---|---|---|
| **Integratore admin** | `is_admin` e `machine_admin` | aprire Odoo, portare il codice nuovo, fare e provare le copie (staging, backup) |
| **Supporto** | ruolo `user`: `can_act` falso | vedere se l'istanza è viva, aprirla, guardare il log, riavviarla |
| **Admin del suo workspace** | `is_admin_here` vero, `scoped`, non `machine_admin` | come l'integratore, ma solo sulle istanze del suo workspace e senza nginx né destinazioni della macchina |
| Developer | ruolo `developer` | come l'integratore, ma non aggiunge e non toglie repository, non salva opzioni, non attiva il ripristino a un minuto |

**Lavoro primario della pagina**: sapere se l'istanza è a posto e aprirla. Il resto, in ordine di frequenza: aggiornare il codice, fare una copia (staging o backup), capire un problema (log, carico, database).

Lavoro primario di ogni tab:

| Tab | Domanda a cui risponde | Chi |
|---|---|---|
| **Ambienti** | «Gira? Le sue copie dove sono?» | tutti |
| **Backup** | «Ho un backup recente che si ripristina?» | developer+ |
| **Log** | «Cosa sta dicendo Odoo adesso?» | tutti |
| **Monitoraggio** | «È lenta: è il carico o il database?» | tutti; la parte database developer+ |
| **Impostazioni** | «Con che codice e che opzioni gira? Le cambio.» | developer+ |

## 2. I tab: al massimo 5

**Ambienti · Backup · Log · Monitoraggio · Impostazioni**

| Oggi | Dove va | Perché |
|---|---|---|
| Tab **Database** | sezione «Database» del tab **Monitoraggio**, sotto il carico | stessa domanda («perché è lenta?»), stessa persona |
| Tab **Addon** | sezione «Codice» di **Impostazioni**, in testa | è l'elenco dei repository: uno solo nella pagina |
| Tab **Configurazione di Odoo** | sezione «Opzioni di Odoo» di **Impostazioni** | si cambiano di rado, e sempre con un riavvio |
| Testa di Ambienti: Worker | Impostazioni › Opzioni di Odoo (è l'opzione `workers`) | duplicato |
| Testa di Ambienti: Database | via | è già nella riga dei fatti della testata |
| Testa di Ambienti: Repository | via; resta un avviso solo se un repository è indietro o fuori branch | terzo duplicato |
| «Percorsi e comandi della shell» | Impostazioni › «Dettagli tecnici ▸» | serve a chi entra via ssh, non al lavoro di ogni giorno |
| Gestione delle destinazioni (S3/SFTP) | **resta fuori dalla pagina**, nella pagina Destinazioni del workspace (`/workspaces/<ws>/destinations`) o della macchina (`/destinations`); qui solo la scelta e il link | è un oggetto del workspace, non dell'istanza |
| Comando root «login-path keep on» | nel dialogo «Entra come…», solo per l'admin della macchina (vedi D3) | compare solo quando serve, cioè quando Entra come fallisce |

**Visibilità dei tab** (stessi meccanismi di oggi: `can_act`, `is_admin_here`, `hidden` tolto dal JS):

| Tab | user | developer | admin del workspace | admin della macchina | Rete di sicurezza |
|---|---|---|---|---|---|
| Ambienti | sì | sì | sì | sì | — |
| Backup | — | sì | sì | sì | solo se il plugin ha i backup |
| Log | sì | sì | sì | sì | — |
| Monitoraggio | se il carico si misura | se il carico si misura **o** il database si legge | idem | idem | non compare vuoto: il JS lo mostra al primo dato |
| Impostazioni | — | sì, in sola lettura dove non può | sì | sì | — |

**Lista HIDE (non si vede di default)**

| Elemento | Dove sta | Rete di sicurezza |
|---|---|---|
| Modulo dei backup automatici | «Backup automatici ▸», chiuso | aperto se i backup automatici sono spenti **e** non c'è nessun backup; la riga di stato sopra dice sempre se sono accesi |
| Copie in una destinazione | «Copie in <destinazione> (N) ▸», chiuso | la riga di stato dice dove va la copia; l'ultimo errore di invio è in rosso, fuori dal `details` |
| Spiegazione del ripristino a un minuto | nel `title` di «Attiva» | — |
| Registro delle modifiche (segmenti, MiB, copie complete) | «Dettagli ▸» della sezione | — |
| Tabelle del database (più grandi, indici mai usati, lette per intero, query più costose) | «Dettagli ▸» di Monitoraggio › Database | una query lenta **non** è nascosta: sta sopra, con «Ferma» |
| PostgreSQL versione, «Dove» | «Dettagli ▸» | — |
| Modulo «Aggiungi un repository» | «+ Aggiungi un repository ▸», chiuso | aperto se l'istanza non ha repository |
| Chiave per ssh | dentro «Aggiungi un repository», come oggi | — |
| Indirizzo e segreto del webhook | sotto «Aggiorna a ogni push», solo da acceso | — |
| Opzioni estese (~60) | interruttore «Mostra tutte le opzioni (N)» | un'opzione estesa con un valore diverso dal default resta visibile anche da spento, con «cambiata» accanto |
| Fatti dell'istanza (tipo, servizi, porta, log, percorsi) | «Dettagli tecnici ▸» in Impostazioni | aggiornamento automatico e «aggiorna i moduli modificati» restano fuori: cambiano il comportamento |

## 3. Regole che UX-006 aggiunge a quelle comuni

1. **Un solo `.primary` visibile nella pagina, ed è quello della testata** (D1). Dentro un tab nessun pulsante è `.primary`. Questo vale per Nuova staging a questo minuto, Aggiungi, Salva e riavvia, e per i pulsanti che il JS mette nella testa di Ambienti (`updateOne`, `newImage`: righe 1111 e 1142). Fa eccezione il pulsante di conferma di un dialogo, che è uno stato a sé. Con un job in corso: 0.
2. **Un salvataggio è disattivato finché non cambi qualcosa**, con accanto «N opzioni cambiate: al salvataggio Odoo si riavvia» (lo schema `oc-changed` esistente). Il colore non lo segnala: lo segnalano il pulsante attivo e il conteggio.
3. **Date sempre `gg/mm/aaaa hh:mm`**, anche quelle formattate in JS. Oggi `toLocaleString()` e `.replace("T", " ")` danno «2026-11-20 10:40» o «20/11/2026, 10:40:12». Serve una sola funzione JS `when()` uguale alla macro Jinja.
4. **Nessun nome tecnico come etichetta.** Il nome tecnico, se serve a chi cerca nella documentazione di Odoo o PostgreSQL, sta dopo in piccolo `mono` (`.small`), mai prima.
5. **Ogni pulsante nuovo ha il `title` della tabella del suo tab.** Un pulsante disattivato ha il `title` su uno `<span>` intorno.

## 4. Tab per tab

### 4.1 Ambienti

**Sopra la piega, in ordine**
1. **Riga di salute**, una sola, al posto del pannello «Versione · Raggiungibile · Worker · Database · Repository»:
   «● Raggiungibile da 20/11/2026 10:39 · 100% nelle ultime 24 ore · risponde in 33 ms · Odoo 19.0 del 12/11/2026».
   - Il `title` sul pallino: «Il monitor di OmniaCLI chiama l'indirizzo di Odoo ogni minuto».
   - Se l'immagine non è la più nuova: «· c'è un'immagine più nuova» con il link «Aggiorna» (secondario `.small`).
2. **Avviso del codice**, solo se serve, in una `note-box`:
   - «2 repository hanno commit nuovi da scaricare: web, Common.» con «Vedi le differenze ↗» per ogni repository e il secondario «Aggiorna…»;
   - «server-backend è sul branch 18.0, atteso 19.0.» con il link «Impostazioni › Codice».
3. **Tabella «Ambienti»** come UX-003 (Produzione, Test, staging), con il secondario «+ Nuova staging» in testa.
4. «Ultima azione: …» come oggi.

| Stato | Primario |
|---|---|
| tutti | quello della testata (D1); dentro il tab nessuno |

**Vuoto**: «Nessuna staging: una staging è una copia datata della produzione, per provare un aggiornamento senza toccarla.» (esiste). Monitor senza dati: «Raggiungibile: ancora nessuna misura (il monitor parte entro un minuto)».

**Pagina della staging**: la `note-box` «Copia di … del gg/mm/aaaa hh:mm, neutralizzata…» con il secondario **«Ricarica dalla produzione…»**, che chiede conferma.

| Pulsante | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|
| staging-refresh | secondario, pericolo, conferma | Ricarica dalla produzione… | «Rimette in questa staging i dati della produzione com'è adesso: un backup nuovo della produzione, caricato qui e neutralizzato (niente posta in uscita, niente azioni pianificate). Il codice della staging resta com'è; i suoi dati di adesso si perdono. Non disponibile mentre un job lavora sulla staging o sulla produzione. Solo admin.» (`/api/servers/{s}/stagings/refresh`) |
| Aggiorna (avviso del codice) | secondario `.small` | Aggiorna… | come «Aggiorna» di UX-003 |
| Vedi le differenze | link ↗ | Vedi le differenze ↗ | «Apre nel repository le differenze fra il commit che gira e l'ultimo del branch» |

### 4.2 Backup

**Sopra la piega, in ordine**
1. **Riga di stato** (`#ab-status` portata in testa), es.:
   «Backup automatici: ogni giorno alle 03:00 (ora del server, UTC) · tiene 7 giorni, 4 settimane, 12 mesi · copia in S3 inaria · ultimo: 20/11/2026 03:00, riuscito · ripristino provato domenica 16/11/2026: riuscito.»
   Spenti: «Backup automatici spenti: nessun backup si fa da solo.» e il `details` sotto aperto.
2. **«Backup automatici ▸»**, chiuso se accesi: il modulo di oggi con le stesse righe, nell'ordine Orario → Conservazione → Copia anche in → Archivio completo → Prova domenicale → Avvisi → «Salva».
3. **Elenco dei backup**. Colonne: File (link che scarica) · Tipo · Data · Dimensione · Ripristino provato · azioni (Prova a ripristinare, Elimina).
4. **«Copie in <destinazione> (N) ▸»**, chiuso.
5. **Sezione «Ripristino a un minuto»** (solo produzione di un'istanza in un workspace, compose; come oggi).

**Testi che sostituiscono il gergo**

| Oggi | Diventa |
|---|---|
| Indietro a un minuto (ripristino point-in-time) | **Ripristino a un minuto** |
| PostgreSQL conserva ogni modifica (i suoi WAL) accanto a un backup di base fatto ogni notte… | *(nel `title` di «Attiva»; a schermo una riga:)* «Riporta il database com'era a un minuto preciso, in una nuova staging: la produzione non cambia.» |
| Si può tornare indietro dal 2026-11-20 10:40 al 2026-11-20 16:35; 1 backup di base, 17 segmenti WAL (256 MiB). | «Puoi scegliere un minuto fra il 20/11/2026 10:40 e il 20/11/2026 16:35. Gli ultimi 5 minuti possono mancare.» Il resto va in «Dettagli ▸»: «1 copia completa, registro delle modifiche 256 MiB (17 file)» |
| Backup di base adesso | **Copia completa adesso** |
| Switch on / Attiva | **Attiva…** (conferma: «Il database si riavvia una volta, per qualche secondo.») |
| Ogni domenica prova anche a ripristinarlo | **Ogni domenica prova a ripristinarlo** |
| Archivio completo, non solo il backup | **Archivio completo (codice e configurazione compresi)** |
| Se fallisce, avvisa | **Se fallisce, avvisa** (resta; il placeholder è già chiaro) |
| Verifica (riga) | **Prova a ripristinare** |
| colonna «Ripristino provato» con ✓/✗ e data ISO | ✓ / ✗ + `gg/mm/aaaa hh:mm`; il `title` di ✓ dice moduli, utenti e ultima modifica (c'è già) |

| Pulsante | Chi | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|---|
| ab-save | chi ha `can_edit` | secondario, attivo solo se il modulo è cambiato | Salva | «Salva orario, conservazione, destinazione e avvisi dei backup automatici di questa istanza. Il prossimo backup parte all'orario scelto, non adesso: per uno subito usa Backup in testata.» |
| ab-dest-link | admin | link | aggiungine una | «Apre le destinazioni del workspace (S3, SFTP…): una destinazione nuova poi si sceglie qui» |
| verifica di riga | developer+ | secondario `.small` | Prova a ripristinare | «Controlla che il file sia integro e lo ripristina per prova in un database a parte accanto all'istanza, poi lo cancella: l'istanza non cambia. È un job; l'esito compare in «Ripristino provato».» (`/backups/verify`) |
| elimina di riga | admin sulla produzione, developer sul test | pericolo, conferma | Elimina… | «Cancella questo file di backup da questa macchina. Non si annulla. Per la produzione serve un admin.» (`/backups/delete`) |
| rlFetch | come sopra | secondario `.small` | Riporta qui | «Riporta questa copia dalla destinazione fra i backup di questa macchina, decifrata. È un job; poi si ripristina da Altro › Ripristina un backup….» (`/remote-backups/fetch`) |
| pitr-enable | admin | secondario, conferma | Attiva… | «Attiva il ripristino a un minuto: il database comincia a tenere il registro di ogni modifica e fa subito la prima copia completa, poi una ogni notte. Il database si riavvia una volta, per qualche secondo: in quei secondi Odoo non risponde. Solo admin.» (`/pitr/enable`) |
| pitr-restore | admin | **secondario** (oggi primario) | Nuova staging a questo minuto | «Crea una nuova staging con il database della produzione com'era al minuto scelto, neutralizzato (niente posta in uscita, niente azioni pianificate); gli allegati sono quelli di adesso. La produzione non cambia. Disattivato se il minuto è fuori dall'intervallo o se un job lavora sulla produzione.» (`/pitr/restore`) |
| pitr-base | admin | secondario | Copia completa adesso | «Fa adesso la copia completa del database da cui parte il ripristino a un minuto; la fa già da sola ogni notte. Dopo un cambio grande accorcia il ripristino. È un job.» (`/pitr/base`) |
| archive-now (Altro › Backup) | developer+ | secondario | Archivio completo adesso | «Salva insieme il backup, il codice di ogni repository al suo commit e la configurazione (compose.yaml, odoo.conf, pacchetti Python): quanto serve per rifare l'istanza su un'altra macchina. Compare fra i backup. È un job.» (`/archive`) |

- Il developer vede la sezione «Ripristino a un minuto» in sola lettura: l'intervallo e la riga «Lo attiva o lo usa un admin.», senza pulsanti. La lettura (GET) è aperta al developer, le azioni solo all'admin.
- **Vuoto**: «Nessun backup ancora. Il primo lo fai con Backup in testata, o accendendo i backup automatici qui sotto.» Ripristino a un minuto spento: «Spento: il database si può riavere solo com'era al momento di un backup.» e «Attiva…».
- **Molte righe**: elenco ordinato dal più nuovo; sopra 10 righe «Mostra tutti (N)».

### 4.3 Log

Resta com'è (UX-005 vale anche qui: livelli e testo del log in inglese ammessi). Due correzioni:
- i chip dei livelli, il filtro e A−/A+ restano in vista, perché qui il log è il lavoro primario;
- «Espandi» ha `title` «Allarga il log a tutta la finestra; Esc torna indietro».

### 4.4 Monitoraggio

**Sopra la piega, in ordine**
1. **Carico** (tutti, se misura): le 3 tile CPU adesso / Memoria / Processi. Ognuna ha un confronto:
   - CPU: «1,2% · su 8 core»;
   - Memoria: «186 MiB · massimo dell'ultima ora 198 MiB»;
   - Processi: il numero.

   Sotto, i 2 grafici come oggi. «Misurato su: the containers of …» diventa `title` sulle tile: «Misurato sui container della produzione».
2. **Database** (developer+), al posto del tab:
   - **una riga di verdetto**, con pallino e parola;
   - **tre tile**;
   - se c'è, **la tabella delle query lente con «Ferma»**;
   - **«Dettagli ▸»** chiuso;
   - «Rileggi» in testa alla sezione.

**Il verdetto** (calcolato nel JS con i dati che `/database` dà già; soglie in D4):

| Verdetto | Quando | Testo |
|---|---|---|
| ● **Il database sta bene** (verde) | nessuna query oltre 5 s, connessioni < 80% del massimo, letto dalla memoria ≥ 95% | «Il database sta bene · 27,5 MiB · PostgreSQL 17» |
| ● **Da guardare** (ambra) | almeno una delle tre fuori soglia | una riga per causa, con il rimedio: «1 query lavora da 2 min 10 s: se blocca gli utenti, fermala qui sotto.» · «92 connessioni su 100: chi apre Odoo adesso può ricevere un errore; un riavvio di Odoo le libera.» · «Il 91% delle letture arriva dalla memoria: il resto va su disco e rallenta.» |
| ● **Non leggibile** (grigio) | la lettura fallisce | «Le statistiche del database non si leggono: <motivo in italiano>.» e «Rileggi» |

**Le tre tile** (i tre numeri del supporto, ognuno col suo confronto):

| Tile | Valore | Sotto |
|---|---|---|
| Connessioni | 3 su 100 | «di PostgreSQL, in uso adesso» |
| Query lente | 0 | «in corso da più di 5 s» |
| Letto dalla memoria | 99,5% | «sotto il 95% rallenta» |

La dimensione e la versione stanno nella riga del verdetto: senza uno storico non hanno un termine di confronto. «Dove» va nei Dettagli.

**Dettagli ▸** (chiuso), con le intestazioni riscritte:

| Oggi | Diventa |
|---|---|
| Le query più costose + messaggio `pg_stat_statements…` | **Le operazioni più costose**. Se manca l'estensione: «Non disponibile: il database non registra le statistiche delle query. Si attiva nella configurazione di PostgreSQL» e, in `.small mono`, `pg_stat_statements` |
| Le tabelle più grandi (15 righe) | **Le tabelle più grandi**, prime 10; colonne Tabella · Righe · Dimensione · Letture con indice · Letture complete · Righe da ripulire · Ultima pulizia (gg/mm/aaaa hh:mm) |
| Indici mai usati | **Indici mai usati**, prime 10 e «Mostra tutti (N)» |
| Tabelle lette per intero | **Tabelle lette tutte senza indice** |
| PostgreSQL / Dove | due righe `dl` in testa ai Dettagli |

| Pulsante | Chi | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|---|
| db-reload | developer+ | secondario `.small` | Rileggi | «Rilegge adesso le statistiche del database. Non cambia niente.» |
| Ferma (riga di query lenta) | `can_stop`: admin sulla produzione | pericolo `.small`, conferma | Ferma… | «Chiude la connessione che esegue questa query: la query si interrompe, la sua transazione si annulla e la richiesta di Odoo che la aspettava riceve un errore. Odoo resta acceso.» (`/database/terminate`, `pg_terminate_backend`). Conferma: «Fermare la query di <utente> in corso da <durata>?» |

- **Vuoto**: carico senza misure e utente `user` → il tab non c'è. Database senza query lente → la tabella non c'è: il verdetto lo dice già.

### 4.5 Impostazioni

Tre sezioni, in quest'ordine:

**A. Codice** (aperta)
- **Una sola tabella dei repository**. Colonne:
  - Repository: nome, e sotto l'indirizzo in `.small`, ↗ se https;
  - Branch: rosso con `title` «atteso 19.0» se diverso;
  - Ultimo commit: sha in `mono`, `gg/mm/aaaa hh:mm`, oggetto troncato a 80 caratteri con `title` intero;
  - Azioni.
- Per un'istanza fuori da un workspace: la stessa tabella **senza azioni** (oggi sta in Impostazioni).
- Sotto, **«+ Aggiungi un repository ▸»** (admin). È chiuso e si apre se l'elenco è vuoto. Dentro: indirizzo, branch, «Aggiungi», e la chiave ssh come oggi.
- Sotto, **«Aggiorna a ogni push»**: interruttore con lo stato a parole, «Spento» oppure «Acceso · ultimo push 20/11/2026 14:02: riuscito». Solo da acceso: indirizzo, segreto (una volta), «Nuovo segreto…». L'aiuto «GitHub: Settings › Webhooks…» va in `details` «Come si collega al repository ▸».

**B. Opzioni di Odoo** (aperta)
- Riga: **etichetta in italiano** prima («Worker»), in `.small mono` il nome (`workers`), poi l'intervallo, poi il valore.
- Normali/Estesi diventa l'interruttore **«Mostra tutte le opzioni (N)»**, spento. Rete di sicurezza: le estese cambiate restano visibili.
- Sotto, a destra, «Salva e riavvia», attivo solo con modifiche, e il conteggio accanto.
- Per developer e admin del workspace senza permesso: campi disattivati e la riga «Le cambia un admin.»

**C. Dettagli tecnici ▸** (chiusa)
- Tipo, Servizio Odoo, Servizio database, Porta HTTP, Log, Servizio, Pacchetto.
- Dentro anche «Percorsi e comandi della shell».
- Restano **fuori** dal `details`, in testa alla sezione A: «Aggiornamento automatico» e «Aggiorna i moduli modificati», perché decidono cosa succede al prossimo push.

| Pulsante | Chi | Tipo | Etichetta | `title` (dal codice) |
|---|---|---|---|---|
| repo update | developer+ | secondario `.small` | Aggiorna… | «Porta il repository all'ultimo commit del branch che scegli (lo stesso: scarica i commit nuovi; un altro: lo cambia e lo ricorda), installa i suoi pacchetti Python, aggiorna i moduli cambiati se l'istanza lo prevede e riavvia Odoo. Non disponibile mentre un job lavora sull'istanza.» (`/repositories/update`) |
| repo remove | admin del workspace | pericolo `.small`, conferma col nome | Elimina… | «Toglie il repository da produzione e test: dal loro odoo.conf e dalla configurazione, cancella le copie del codice e li riavvia. I moduli già installati da qui restano nel database senza il loro codice: disinstallali prima da Odoo › App. Solo admin.» (`/repositories/remove`) |
| Aggiungi (dentro il `details`) | admin del workspace | **secondario** (oggi primario) | Aggiungi | «Scarica il repository nella produzione e nel test, ognuno sul suo branch, lo aggiunge al loro odoo.conf e li riavvia: i suoi moduli poi si installano da Odoo › App. Se è privato: via https usa le credenziali git del workspace, via ssh (git@…) la chiave qui sotto.» (`/repositories/add`) |
| addons-key-show | admin del workspace | secondario `.small` | Mostra la chiave | «Mostra la chiave pubblica con cui OmniaCLI legge i repository via ssh (la crea la prima volta): aggiungila al repository come chiave di deploy in sola lettura.» (`/git-key`) |
| dh-enabled | `can_edit` | interruttore | Aggiorna a ogni push | «Acceso: un push sul branch di uno dei suoi repository lo scarica, aggiorna i moduli cambiati (prima un backup, se l'istanza lo chiede) e riavvia. Spento: il webhook del repository non fa più niente.» (`/deploy-hook`) |
| dh-new | `can_edit` | secondario `.small`, conferma | Nuovo segreto… | «Crea un segreto nuovo e annulla quello di prima: il webhook del repository smette di funzionare finché non ci incolli il nuovo. Il segreto si vede una volta sola.» |
| op-save / oc-save | admin | **secondario**, attivo con modifiche | Salva e riavvia | «Scrive le opzioni cambiate nell'odoo.conf e riavvia Odoo: chi è collegato perde la connessione per qualche secondo. Disattivato finché non cambi un'opzione.» |
| Mostra tutte le opzioni | admin, developer | interruttore | Mostra tutte le opzioni (N) | «Mostra anche le opzioni che si cambiano di rado; quelle già cambiate si vedono sempre» |

### 4.6 Testata e dialogo «Entra come…»

**Testata**: invariata rispetto a oggi, cioè Apri Odoo · Entra come… · Aggiorna · Altro ▾ (D1 decide il primario). Il pannello «Altro» segue UX-003.

**Dialogo «Entra come…»** quando nginx non lascia passare il percorso:
- **Admin della macchina**:
  - testo: «Il sito di <host> non porta Entra come a OmniaCLI: lo apre Odoo.»;
  - primario del dialogo: «Fai passare da nginx». `title`: «Aggiunge una riga al sito nginx di <host> (il file di prima resta salvato), controlla nginx e lo ricarica. Si toglie con workspace-helper login-path remove. Solo admin della macchina.»;
  - sotto, il `details` **«Il sito lo riscrive un altro programma ▸»** con il secondario di D3, oppure il comando root com'è oggi.
- **Gli altri**: «Entra come non è ancora attivo per questo indirizzo: chiedi all'amministratore della macchina.» Nessun comando a schermo.

## 5. Il necessario

- **Stato del sistema**:
  - stato in testata;
  - riga di salute in Ambienti, riga di stato dei backup, verdetto del database;
  - fascia del job;
  - ogni azione qui sopra è un job e la fascia lo mostra; a job finito la sezione si ricarica.
- **Annulla**: è una conferma su ogni azione che non si annulla. Riguarda Elimina backup, Elimina repository, Ferma query, Nuovo segreto, Ricarica dalla produzione, Attiva ripristino a un minuto, Salva e riavvia. Le opzioni non salvate si scartano lasciando il tab (oggi non c'è un avviso: aggiungerlo con `beforeunload` solo se il conteggio è > 0).
- **Prevenzione dell'errore**:
  - il minuto fuori intervallo disattiva «Nuova staging a questo minuto», con il motivo nel `title`;
  - il salvataggio è disattivato senza modifiche;
  - un campo numerico fuori dall'intervallo (`min`/`max`, già presenti) diventa rosso prima del clic;
  - l'Elimina di un repository chiede il nome.
- **Messaggio d'errore**: cosa non è andato e cosa fare, in italiano; mai `data.error` di `refuse()` grezzo (regola comune). Oggi il JS fa `alert(data.error || …)` in 6 punti: passano dal traduttore `tr`, oppure il backend restituisce la chiave.
- **Aiuto**: il `title` di ogni pulsante nelle tabelle sopra; i `details` «Come si collega al repository ▸» e «Dettagli ▸».

## 6. Criteri di accettazione (Playwright)

Come **admin della macchina**, **admin del workspace**, **developer** e **user**. Viewport 1440×900 e 375×812, lingua IT. Istanza in un workspace, compose, in esecuzione, con almeno 2 backup e 2 repository. **Non** come developer per le schermate di accettazione del supporto.

1. `.tabs [role=tab]:visible` ≤ 5 per ogni persona. Admin: esattamente Ambienti, Backup, Log, Monitoraggio, Impostazioni. User: Ambienti, Log, più Monitoraggio solo se `[data-tile=cpu]` ha un valore.
2. Per ogni tab aperto, `.primary:visible` fuori da `dialog[open]` = 1, quello della testata (0 con `.job-band` presente). Dentro `dialog[open]`: ≤ 1.
3. Ogni `button:visible`, `a.button:visible` e `summary:visible` ha `title` non vuoto e diverso dal testo, oppure un `span[title]` genitore se è `disabled`. Sono 0 senza aiuto (oggi 20).
4. Nessun match di `/\d{4}-\d{2}-\d{2}/` nel testo visibile fuori da `[data-panel=log]`. Nessun match di `/\b(the|of|containers|WAL|base backup|vacuum|point-in-time|pg_stat_statements)\b/i` nel testo visibile **fuori** da `details:not([open])`, da `.mono.small` e dal Log.
5. Monitoraggio come admin: `[data-panel=monitor]` alto ≤ 1100 px con «Dettagli» chiuso. Esiste un elemento verdetto con una delle tre parole («Il database sta bene», «Da guardare», «Non leggibile»). Le tile del database sono esattamente 3 (Connessioni, Query lente, Letto dalla memoria). `table` visibili fuori dai Dettagli = 0, salvo la tabella delle query lente quando `long_queries` non è vuota.
6. In tutta la pagina c'è al massimo 1 tabella con un'intestazione «Repository» (Impostazioni › Codice). In Ambienti nessuna riga «Repository» e nessuna riga «Database».
7. Backup come admin, con i backup automatici accesi: `#ab-form` dentro un `details` chiuso; la riga di stato e la prima riga dell'elenco sono sopra i 900 px. Spenti e senza backup: il `details` è aperto.
8. Impostazioni › Opzioni di Odoo: con l'interruttore spento, righe visibili = opzioni normali + opzioni estese cambiate. «Salva e riavvia» `disabled` finché non si cambia un campo, poi attivo con il conteggio «1 opzione cambiata». In ogni riga l'etichetta italiana precede il nome tecnico.
9. User sulla produzione: assenti Backup, Impostazioni, «+ Nuova staging», «Ferma…», «Rileggi». Admin del workspace: nel dialogo Entra come, assenti «Fai passare da nginx» e il comando root. Developer: sezione Ripristino a un minuto senza pulsanti.
10. Ogni conferma elencata in §5 compare: `page.on("dialog")` o un `dialog[open]`, con il nome dell'oggetto nel testo. Annullando, nessuna richiesta POST parte.

## 7. Ordine di implementazione (per impatto)

1. **Tab da 8 a 5 e un solo primario**: Database in Monitoraggio, Addon e Configurazione in Impostazioni; via i `.primary` interni (incluse le righe 1111 e 1142); via i due elenchi dei repository in più. È il cambio che Carlo vede per primo. Criteri 1, 2, 6.
2. **Verdetto del database con tre tile e «Dettagli»**: solo JS sui dati che ci sono già. Criterio 5.
3. **Backup**: riga di stato in testa, modulo nel `details`, testi del ripristino a un minuto. Criterio 7.
4. **`title` su tutti i pulsanti, date `gg/mm/aaaa`, niente inglese** (funzione JS `when()`, «Misurato su», `alert` tradotti). Criteri 3 e 4.
5. **Opzioni di Odoo** con l'etichetta prima e l'interruttore; **Entra come** con D3. Criteri 8 e 9.

## 8. Decisioni di Carlo

1. **D1 — Il primario della testata.** UX-003, approvato, diceva «Aggiorna» per developer e admin; il codice di oggi ha «Apri Odoo» per tutti.
   - **Raccomandata: «Apri Odoo» per tutti, come oggi** (con la produzione ferma: «Avvia»). Le tre persone vedono lo stesso primario nella stessa posizione. «Aggiorna» resta secondario accanto: un clic in più di riflessione prima di cambiare il codice della produzione. UX-003 va corretto in quella riga.
   - Alternativa: «Aggiorna» come da UX-003. Il supporto (ruolo `user`) ha «Riavvia» come primario, l'integratore «Aggiorna», e «Apri Odoo» diventa secondario: il primario cambia da persona a persona.
2. **D2 — Il nome del tab 4.**
   - **Raccomandata: «Monitoraggio»**, come nel mockup approvato il 18/11.
   - Alternativa: «Salute». Dice meglio la domanda («sta bene?») ma cambia una parola già approvata e lo screenshot del mockup.
3. **D3 — «Tieni la riga in ogni sito» (oggi il comando root `omniacli workspace-helper login-path keep on`).**
   - **Raccomandata: un pulsante nel `details` del dialogo Entra come**, solo per l'admin della macchina. Serve **una route nuova** di poche righe che chiama l'helper con `sudo -n`, come già fa «Fai passare da nginx». L'helper conosce già `keep on`/`off`. Effetto: un timer di root che ogni minuto rimette la riga in tutti i siti del servizio, anche nei nuovi. Per spegnerlo, lo stesso pulsante con `keep off`.
   - Alternativa: nessun backend; il comando resta nel `details`, copiabile. Chi non ha ssh di root sulla macchina non lo può usare.
4. **D4 — Soglie del verdetto del database.**
   - **Raccomandate: query lenta > 5 s (quella che il rapporto conta già), connessioni ≥ 80% del massimo, letto dalla memoria < 95%.** Sull'istanza misurata oggi (3 su 100, 99,49%, 0 query lente) il verdetto è verde.
   - Alternativa: soglie tue per cliente, da mettere in configurazione. È una funzione di backend: fuori da questo contratto.

## Non verificato

- Le schermate sono quelle dell'admin. Il tab Monitoraggio per il ruolo `user` (cioè se `/uptime` e i grafici rispondono senza `developer`) l'ho dedotto dal template, non l'ho misurato. Anche la vista a 375×812 non è misurata.
- «Odoo resta acceso» durante «Copia completa adesso»: l'helper `pitr-base` non l'ho letto riga per riga, quindi nel `title` non l'ho scritto.
- Le copie già mandate a una destinazione restano dopo «Elimina» di un backup locale? Non ho letto `backups/delete` fino in fondo, quindi il `title` non lo promette.
- D3: che il sudoers della macchina permetta `workspace-helper login-path keep` come `login-path add`.
- Il numero «20 pulsanti senza `title`» è la misura del chiamante; le tabelle sopra coprono quelli che ho trovato in `server.html` e nei suoi JS. Quelli dei plugin (`action_button`) vanno contati dal criterio 3.

## Tracciabilità

Obiezione di Carlo del 20/11/2026 («gli manca proprio l'usabilità»); misura Playwright del 20/11 (schermate `ux-*.png`). Estende UX-003/004 (`UX-001-hosting-panel-parity.md`) e il mockup `3-istanza.html`; criteri comuni di UX-001 §«Criteri di accettazione». Nessun ticket `T`/`HT` collegato.
