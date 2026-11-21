# UX-008 — Le funzioni nuove viste sul pannello di riferimento

Stato: contratto di design, prima della build. Implementa chi scrive i template di `omniacli/templates/` (Jinja + JS senza framework, classi esistenti `.tabs`, `.panel`, `.primary`, `.small`, `details`, `dialog`). Le stringhe vanno in `omniacli/i18n/it/*.json`. Le route da creare sono segnate **(route da creare)** e riassunte in §9.
Valgono le regole comuni e i criteri di UX-001…007: questo contratto li applica e non li ripete.
Origine: Carlo, 21/11/2026, guardando dal vivo il pannello di riferimento (note: `confronto-pannello-21-11.md`, 28 schermate). Decisioni dello stesso giorno: la testata dell'istanza è neutra e il primario di ogni scheda è l'azione che crea; si costruiscono (A) elenchi globali, (B) staging migliori, (C) indirizzi, database e monitoraggio, (D) Marketplace.
Riferimento: il pannello di riferimento, schermate 01-28 del 21/11/2026.

## 0. Cosa c'è oggi (21/11/2026)

| Funzione | Oggi in OmniaCLI | Cosa manca |
|---|---|---|
| Elenco delle istanze | La Dashboard `/` mostra le istanze di questa macchina. «Collegate» mostra le altre macchine solo con CPU e rete. Le istanze di un collegamento si vedono aprendo `/links/<nome>/` | Un elenco unico di tutte le macchine. L'avviso «nessun backup programmato» |
| Elenco dei backup | Scheda Backup di ogni istanza (`/backups`) | Un elenco unico, con l'esito degli automatici falliti |
| Ripristino in un'istanza nuova | Solo «Ripristina un backup…» in Altro, che **sostituisce** il database. C'è anche «Crea dall'archivio» nel workspace, ma solo da un archivio completo | Ripristinare un backup normale senza toccare l'istanza |
| Staging | «+ Nuova staging» crea la copia sulla stessa macchina e nello stesso workspace, senza domande. «Ricarica dalla produzione…» riporta solo i dati | La scelta della macchina. Una staging che torna uguale alla produzione anche nel codice. La posta: oggi `odoo neutralize` la spegne e basta |
| Indirizzi | Workspace › Nomi sul web: dominio del workspace, istanza principale, sottodomini `<sub>.<dominio>`, certificato. HTTPS sempre forzato | Un dominio fuori dal workspace (es. www.cliente.it). Un reindirizzamento (cliente.it → www.cliente.it) |
| PostgreSQL | Un container `postgres:17` per ogni ambiente, con i valori di base dell'immagine (128 MB di memoria condivisa). Monitoraggio › Database lo legge e basta | Una regolazione qualunque |
| Monitoraggio della macchina | Dashboard › Monitoraggio: ultima ora in memoria (`metrics.HISTORY = 720`). Lo storico dei dischi è giornaliero, fino a 3 anni (`diskhistory`) | CPU, memoria e rete oltre l'ora |
| Moduli | Impostazioni › Codice: si aggiunge un repository per indirizzo (`/repositories/add`, che riavvia produzione e test); poi si installa da Odoo › App | Cercare un modulo e installarlo |

## 1. Persone e lavoro primario

| Persona | Chi è nel codice | Cosa usa di questo contratto |
|---|---|---|
| **Integratore, admin della macchina** (Carlo) | `is_admin`, non `scoped` | tutto |
| **Admin di un workspace** | `scoped`, ruolo admin nel workspace | Istanze e Backup filtrati sulle sue istanze di questa macchina; staging, indirizzi, database e moduli delle sue istanze |
| **Developer** | ruolo `developer` | Istanze, Backup (legge e scarica), Posta, Monitoraggio, catalogo in sola lettura; installa solo in Test (D3) |
| **Supporto** | ruolo `user` | Istanze (lettura), Posta della staging, Monitoraggio |

Device: desktop 1440×900. Il telefono (375×812) serve solo a leggere Istanze e Monitoraggio.

| Schermata | Domanda a cui risponde | Primario |
|---|---|---|
| **Istanze** (`/instances`) | «Le istanze di tutte le macchine girano, e hanno un backup?» | **+ Nuova istanza** |
| **Backup** (`/backups`) | «I backup di stanotte ci sono tutti? Me ne serve uno.» | nessuno: la pagina legge. Il primario sta nel dialogo di riga |
| Dialogo **Nuova staging** | «Una copia della produzione, dove?» | **Crea la staging** (del dialogo) |
| Staging › **Posta** | «Cosa avrebbe mandato Odoo, e a chi?» | nessuno |
| Dialogo **Indirizzi…** | «A quali nomi risponde, e quale porta dove?» | **Salva e pubblica** (del dialogo) |
| Impostazioni › **Database** | «Il database è regolato per la memoria che ha?» | il primario della scheda resta «Salva e riavvia» |
| Macchina › **Monitoraggio** | «La macchina basta, o in queste due settimane è stata al limite?» | nessuno |
| Istanza › **Moduli** | «Che moduli ha? Ne aggiungo uno.» | **+ Aggiungi moduli** |

## 2. Regole che UX-008 aggiunge a quelle comuni

1. **La regola del primario (sostituisce la regola 1 di UX-006 §3: testo in §10).** Ogni pagina ha una testata neutra. In ogni scheda, o pagina senza schede, c'è al più un `.primary`, ed è l'azione che crea. Dove niente si crea (Log, Monitoraggio, Backup globale, Posta) il primario non c'è. Il pulsante di conferma di un dialogo è il primario del dialogo.
2. **Un elenco che somma più macchine dice quale non ha risposto.** Una macchina che non risponde entro il tempo del collegamento diventa una riga «<nome> non risponde: <motivo>. Le sue istanze qui non ci sono.». Non sparisce in silenzio, e non blocca le altre righe.
3. **Un numero in testa è un filtro.** «3 senza backup programmato» è un link: filtra l'elenco, e il conteggio dell'elenco filtrato è lo stesso.
4. **Una copia è sempre neutralizzata.** Nessun dialogo di questo contratto offre una copia non neutralizzata: la posta e le azioni pianificate partirebbero in doppio con la produzione.
5. **Un nome tecnico (parametro di PostgreSQL, nome di modulo, repository) sta dopo l'etichetta italiana, in `.small mono`.** Mai al posto dell'etichetta (regola 4 di UX-006).
6. **Un'azione che riavvia la produzione lo dice nel `title` e nella conferma**, con «chi è collegato perde la connessione per qualche secondo». Vale anche per «Aggiungi moduli», che aggiunge repository.

## 3. Visibilità

| Elemento | admin macchina | admin workspace | developer | user | Rete di sicurezza |
|---|---|---|---|---|---|
| Menu **Istanze** → `/instances` | sì, tutte le macchine | sì, solo le sue (`visible_servers`), senza colonna Server | sì | sì | la Dashboard `/` resta raggiungibile da Server › questa macchina e dal logo |
| Menu **Backup** → `/backups` | sì | le sue istanze | sì, legge e scarica | — | — |
| Colonna Server nelle due pagine | sì | — | se le macchine sono più di una | idem | compare appena le macchine sono due |
| Nuova staging › scelta della macchina | sì | — (solo questa macchina, senza la scelta) | — | — | se l'unica macchina compatibile è questa, la scelta non compare: c'è la riga «Su questa macchina» |
| Staging › scheda **Posta** | sì | sì | sì | sì | solo sulle staging che hanno la posta catturata; le altre dicono «posta spenta» nella nota |
| ⚙ **Indirizzi…** nella tabella Ambienti | sì | sì | — (vede gli indirizzi, non il pulsante) | — | — |
| Impostazioni › **Database** | sì | sì | sola lettura | — | solo per le istanze compose; altrimenti la sezione non c'è |
| Monitoraggio della macchina, 14 giorni | sì | — (come oggi: nessun carico della macchina per chi è `scoped`) | — | — | — |
| Scheda **Moduli** | sì | sì | sì; installa solo in Test | — | per un'istanza fuori da un workspace: la tabella dei repository senza azioni né catalogo |

## 4. A — Elenchi globali

### 4.1 Istanze (`/instances`)

**Testata**: «Istanze» e la riga dei conteggi «32 istanze · 9 staging · **3 senza backup programmato**» (l'ultimo è un filtro, regola 3). A destra il primario **+ Nuova istanza**.

**Sopra la piega**: ricerca (solo oltre 8 righe), poi la tabella.

| Colonna | Contenuto |
|---|---|
| Istanza | L'indirizzo in grassetto, link alla sua pagina (`/servers/…` o `/links/<n>/servers/…`). Le staging stanno subito sotto la loro produzione, come «↳ indirizzo [Staging]». Test non è una riga (UX-001 §0) |
| Server | Il nome della macchina e la versione di Odoo come badge («19.0») |
| Stato | Pallino e parola, con i 4 valori comuni |
| Backup | Produzione con backup automatici: «ultimo 21/11/2026 03:00». Ultimo fallito: «✗ ultimo non riuscito 21/11/2026 03:00», in rosso. Nessun backup automatico: «⚠ Nessun backup programmato», in ambra, link alla scheda Backup dell'istanza. Staging: «—» con `title` «Le staging sono copie: non hanno backup programmati» |

- Nessun pulsante nelle righe (UX-002).
- **Molte righe**: ordine per macchina e poi per indirizzo; ricerca sopra 8; paginazione sopra 50.
- **Vuoto**: «Nessuna istanza su nessuna macchina. + Nuova istanza ne crea una dalla sola versione di Odoo.»
- **Macchina che non risponde**: una riga di gruppo (regola 2).

**HIDE**: Edizione (Community o Enterprise; si vede se ce ne sono due diverse); Workspace (sta nella ricerca); Test come riga; i worker.

**Dialogo Nuova istanza** (lo stesso di `_new_instance_dialog.html`, più la scelta del server):
- **Server**: radio. Questa macchina è la prima e già scelta. Seguono i collegamenti che rispondono e hanno almeno una versione (`/links/<n>/api/instances/versions`). Sotto, una riga muta: «Non elencati: ber (non risponde), ridix (nessuno spazio per i workspace).»
- Poi i campi di oggi: Versione, Nome, Dominio, Lingua. La password dell'admin e il paese stanno in «Avanzate ▸».
- **Crea** (primario del dialogo) · Annulla.

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| nuova istanza | admin macchina | **primario** | + Nuova istanza | «Crea una nuova istanza Odoo sul server che scegli, dalla sola versione: un workspace suo, una produzione e un test; con un dominio, la produzione risponde lì e il test su test.<dominio>. È un job.» (`/api/instances/new` sul server scelto, attraverso il suo collegamento) |
| filtro | tutti | link | 3 senza backup programmato | «Mostra solo le produzioni senza backup automatici: un guasto del disco le perderebbe» |

### 4.2 Backup (`/backups`)

**Testata**: «Backup» e la riga «212 backup · 1,3 TB · stanotte: 31 riusciti, 1 non riuscito». «1 non riuscito» è un filtro. Nessun primario.

| Colonna | Contenuto |
|---|---|
| Istanza | L'indirizzo, link alla scheda Backup dell'istanza; badge della versione |
| Fatto il | `gg/mm/aaaa hh:mm` e la parola «automatico», «prima di un aggiornamento» o «a mano» (dal `kind` di oggi) |
| Dimensione | «1,2 GB», allineata a destra |
| Esito | «Pronto». Con la prova: «✓ ripristino provato 16/11/2026» o «✗ ripristino non riuscito». Un automatico fallito diventa una riga a sé: «Non riuscito: <motivo in italiano>», con link al job |
| Azioni | menu «Azioni ▾»: Scarica · Ripristina in un'istanza nuova… · Elimina… |

- **Molte righe**: dal più nuovo; paginazione da 50; ricerca per istanza.
- **Vuoto**: «Nessun backup su nessuna macchina. I backup automatici si accendono nella scheda Backup di ogni istanza.»
- **HIDE**:
  - «Ripristina sopra l'istanza»: resta solo nella pagina dell'istanza, dove chiede di scrivere il nome del database;
  - il nome del file (sta nel `title` della data);
  - le copie nelle destinazioni (restano nella scheda dell'istanza).

**Dialogo «Ripristina in un'istanza nuova»** (D6, raccomandata):
- Riga: «Backup di essetech.it del 21/11/2026 03:00 · 1,2 GB».
- Frase fissa: «Diventa una staging di essetech.it, neutralizzata: niente posta in uscita, niente azioni pianificate. La produzione non cambia.»
- «Avanzate ▸» contiene la scelta del server, come nella Nuova staging (§5.1).
- Prevenzione: un backup con «✗ ripristino non riuscito» disattiva il primario. Il motivo sta nel `title` dello `span`: «Questo backup non si è ripristinato alla prova del 16/11/2026: scegline un altro».
- **Crea la staging da questo backup** (primario del dialogo) · Annulla.

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| scarica | developer+ | voce di menu | Scarica | «Scarica il file zip del backup (database e allegati) su questo computer. Non cambia niente.» (`/backups/{s}/{role}/{file}`) |
| ripristina nuova | admin | voce di menu | Ripristina in un'istanza nuova… | «Crea una staging nuova della produzione con il database di questo backup, neutralizzato. La produzione e il backup non cambiano. È un job.» **(route da creare: `POST /api/servers/{s}/stagings/from-backup`, `new_staging` con `load` = ripristino del file)** |
| elimina | admin sulla produzione, developer sul test | voce di menu, pericolo, conferma | Elimina… | «Cancella questo file di backup da questa macchina. Non si annulla. Per la produzione serve un admin.» (`/backups/delete`). Conferma: «Eliminare il backup di essetech.it del 21/11/2026 03:00?» |

## 5. B — Staging migliori

### 5.1 Dialogo «Nuova staging» (oggi il clic crea subito)

Si apre da «+ Nuova staging», primario di Ambienti.
- Riga in testa: «Copia di essetech.it com'è adesso: circa 2,3 GB.» Il numero viene da `/backup/estimate`, che esiste.
- **Server** (solo admin della macchina): radio.
  - «Questa macchina (inaria)» è già scelta, poi i collegamenti con **lo stesso template** della produzione (es. `odoo-19`) e spazio sufficiente.
  - Sotto, una riga muta: «Non elencati: ber (ha solo Odoo 18), ridix (non risponde).»
  - Se l'unica macchina compatibile è questa, il campo non c'è.
- **Indirizzo**: in sola lettura, «stg-20261121.essetech.it» (la regola di oggi). Su un'altra macchina: «stg-20261121.<dominio del workspace là>».
- Frase fissa: «Neutralizzata: niente azioni pianificate; la posta in uscita si legge nella scheda Posta, non parte.»
- «Avanzate ▸» (solo con un'altra macchina): Workspace là. Il default è quello con lo stesso nome, creato se manca.
- **Crea la staging** (primario del dialogo) · Annulla.

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| staging-new | admin del workspace | **primario** di Ambienti | + Nuova staging | «Apre il modulo per una copia della produzione com'è adesso, neutralizzata, a un indirizzo suo: su questa macchina o su un'altra con la stessa versione di Odoo. La produzione resta accesa. Non disponibile mentre un job lavora sulla produzione.» |
| crea | idem | primario del dialogo | Crea la staging | Questa macchina: «Fa un backup della produzione, crea la staging dallo stesso template e ci carica il backup neutralizzato. È un job.» (`POST /api/servers/{s}/stagings`). Altra macchina: «… porta il backup sull'altra macchina attraverso il collegamento e crea là la staging.» **(route da creare: `POST /api/servers/{s}/stagings` con `machine`; il record della staging là deve sapere che la sua produzione è su un'altra macchina)** |

**Errori** (da `refuse()`, tradotti):
- «Il workspace X ha già tutte le sue N istanze: alza il limite (Limiti e disco) o elimina una staging.»
- «Sull'altra macchina non c'è spazio: servono circa 4,6 GB, liberi 3,1 GB.»
- «Il template da cui è nata la produzione non è più su questa macchina.»

### 5.2 «Ricostruisci…» (pagina della staging, nella nota in testa)

Prende il posto di «Ricarica dalla produzione…», nello stesso punto (accanto alla data che cambia). Ha un dialogo con una scelta (D9):
- ● **Solo i dati** (default): «Il codice della staging resta: per provare il codice nuovo sui dati di oggi.»
- ○ **Dati e codice**: «La staging torna uguale alla produzione: i repository tornano ai commit della produzione, e quello che provavi qui si perde.»
- Il dialogo ha due pulsanti, **Ricostruisci** (primario del dialogo, pericolo) e Annulla. Sopra di loro la riga «I dati di adesso della staging si perdono. L'indirizzo resta lo stesso.»

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| staging-rebuild | admin del workspace | secondario `.small`, pericolo, dialogo | Ricostruisci… | «Rifà questa staging dalla produzione com'è adesso: un backup nuovo, caricato qui e neutralizzato; a scelta anche il codice, ai commit della produzione. Il suo indirizzo resta. Non disponibile mentre un job lavora sulla staging o sulla produzione. Solo admin.» Solo i dati: `/stagings/refresh`, che esiste. Dati e codice: **(route da creare: `/stagings/refresh` con `code: true`)** |

### 5.3 Scheda «Posta» (pagina della staging)

Posizione: dopo Panoramica. Le schede della staging diventano Panoramica · Posta · Backup · Log · Monitoraggio · Impostazioni.

- Riga di stato: «Catturata: la posta che Odoo di questa staging manda arriva qui e non parte. 14 messaggi, l'ultimo 21/11/2026 10:42.»
- Tabella: Ricevuta (`gg/mm/aaaa hh:mm`) · A · Oggetto (troncato a 80 caratteri, `title` intero). Il mittente sta in `.small` sotto l'oggetto. Dal più nuovo; gli ultimi 50, poi «Mostra tutti (N)».
- Il clic su una riga apre un `dialog` con:
  - Da, A, Cc, data;
  - il messaggio in un `iframe sandbox` (senza script);
  - i nomi degli allegati, scaricabili.
- Nessun primario. «Rileggi» è un secondario `.small`; la scheda si rilegge anche da sola ogni 10 s mentre è aperta. «Svuota…» è un pericolo `.small` con conferma.
- **Vuoto**: «Nessun messaggio. Quello che Odoo di questa staging manda (fatture, inviti, avvisi) arriva qui e non parte.»
- **Rete di sicurezza**: le staging fatte prima di questa funzione non hanno la scheda. La loro nota dice «neutralizzata: nessuna posta in uscita» come oggi. Ricostruisci, con «Dati e codice», la attiva.
- **HIDE**: le intestazioni grezze del messaggio e l'indirizzo del servizio che cattura la posta.
- Implementazione:
  - un servizio SMTP di cattura con API di lettura (per esempio Mailpit, licenza MIT) nel progetto compose della staging, raggiungibile solo dall'Odoo della staging;
  - `smtp_server` del suo odoo.conf punta lì;
  - `odoo neutralize` spegne già i server di posta del database, quindi Odoo usa quello dell'odoo.conf;
  - nessuna porta pubblica nuova: la scheda legge attraverso OmniaCLI.

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| rileggi | tutti | secondario `.small` | Rileggi | «Rilegge adesso la posta catturata. Non cambia niente.» |
| svuota | admin del workspace | pericolo `.small`, conferma | Svuota… | «Cancella tutti i messaggi catturati di questa staging. Non tocca Odoo né la sua coda di posta.» **(route da creare: `POST …/mail/clear`)** |
| riga | tutti | riga cliccabile | — | «Apre il messaggio com'era, senza mandarlo» **(route da creare: `GET /api/servers/{s}/production/mail`, `…/mail/{id}`)** |

## 6. C — Indirizzi, database, monitoraggio

### 6.1 Dialogo «Indirizzi…» (⚙ nella riga di Ambienti)

Ogni riga della tabella Ambienti (Produzione, Test, staging) ha in fondo un ⚙ `.small` «Indirizzi…». Lo vede solo l'admin del workspace.

- **Indirizzo principale**: in sola lettura, dalla regola del workspace (es. «essetech.it»), con ↗. Sotto: «Lo decide il workspace (Nomi sul web).»
- **Altri indirizzi**: una riga per nome, con il suo stato DNS:
  - «✓ punta qui»;
  - «✗ punta a 5.6.7.8: crea un record A verso 1.2.3.4».

  Con «+ Aggiungi un indirizzo». Vale un nome qualunque (www.cliente.it) o un sottodominio del workspace.
- **Reindirizzamenti**: «cliente.it → porta a www.cliente.it», con «+ Aggiungi un reindirizzamento». Il «porta a» si sceglie fra gli indirizzi qui sopra.
- **Frase fissa, nessun interruttore**: «Sempre su HTTPS: chi apre http:// viene portato su https://.» È già così: `conf_text` risponde 301 su https a ogni nome.
- **Salva e pubblica** (primario del dialogo, attivo solo con modifiche) · Annulla.
- **Prevenzione**:
  - un nome già usato da un altro sito di questa macchina è rosso prima del clic: «già usato da <sito>» (`names_elsewhere`, che esiste);
  - un nome senza DNS si può salvare, ma la riga dice «resta fuori dal certificato finché il DNS non punta qui».
- **Vuoto**: «Solo l'indirizzo principale. Un altro nome (www.cliente.it) o un reindirizzamento si aggiungono qui sotto.»

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| ⚙ di riga | admin del workspace | secondario `.small` | Indirizzi… | «I nomi a cui risponde questo ambiente e i reindirizzamenti verso di lui» |
| aggiungi indirizzo | idem | secondario `.small` | + Aggiungi un indirizzo | «Un altro nome che apre questo ambiente: il DNS del nome deve puntare a questa macchina. Vale con Salva e pubblica.» |
| aggiungi redirect | idem | secondario `.small` | + Aggiungi un reindirizzamento | «Un nome che porta i visitatori su un altro indirizzo di questo ambiente, con lo stesso percorso. Vale con Salva e pubblica.» |
| salva | idem | primario del dialogo | Salva e pubblica | «Salva i nomi, riscrive il sito nginx del workspace e chiede il certificato anche per i nomi nuovi. Odoo non si riavvia. È un job. Un nome senza record DNS resta fuori dal certificato.» **(route da creare: `/web/save` con `aliases` e `redirects` per istanza e ruolo; `web_names` e `conf_text` con i blocchi 301; `POST /web/check-names` per lo stato DNS)** |

### 6.2 Impostazioni › «Database» (per istanza, D2)

È una sezione nuova fra «Opzioni di Odoo» e «Dettagli tecnici». Solo per le istanze compose.

- **Riga di stato, sempre in vista**, con pallino e parola:
  - ambra: «Valori di base di PostgreSQL: pensati per una macchina piccola. 128 MB di memoria condivisa, su 2 GB che il database può usare.»;
  - verde: «Regolato per 2 GB il 21/11/2026 10:00.»
- Secondario **«Regola sulla memoria…»**. Apre un dialogo con:
  - la base del calcolo, in una riga: «Calcolato su 2 GB: il limite di memoria del workspace odoo19 (4 GB), diviso fra i suoi 2 database (produzione e test).» Senza limite, la memoria della macchina divisa fra i database della macchina;
  - la tabella Parametro · Adesso · Proposto. Le righe che cambiano sono in grassetto; quelle che chiedono un riavvio hanno la parola «riavvio»;
  - accende anche le statistiche delle query, quelle che Monitoraggio › Database dice «non disponibili»;
  - **Applica e riavvia il database** (primario del dialogo) · Annulla. Prima la conferma: «Il database si riavvia: per qualche secondo Odoo non risponde.»
- **«Parametri (6) ▸»**, chiuso. Ogni riga ha l'etichetta italiana, il nome in `.small mono`, «default: …» e il valore. Le righe di base sono:
  - Connessioni al massimo `max_connections`;
  - Memoria condivisa `shared_buffers`;
  - Memoria che il sistema tiene in cache `effective_cache_size`;
  - Memoria per operazione `work_mem`;
  - Memoria per la manutenzione `maintenance_work_mem`;
  - Costo di una lettura sparsa `random_page_cost`.

  «Mostra tutti i parametri (N)» è spento. Un parametro avanzato cambiato resta visibile anche da spento.
- Il salvataggio è quello della scheda: «Salva e riavvia» (primario di Impostazioni). Il conteggio dice cosa riavvia: «1 opzione di Odoo, 2 del database: si riavviano Odoo e il database».
- **HIDE e protezione**: i parametri del ripristino a un minuto (`archive_mode`, `archive_command`, `wal_level`) non si vedono e non si regolano qui. Lo fa «Attiva…» del ripristino a un minuto.

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| regola | admin del workspace | secondario | Regola sulla memoria… | «Propone i parametri di PostgreSQL per la memoria che questo database può usare; li vedi prima di applicarli. Applicati, il database si riavvia.» **(route da creare: `GET/POST /api/servers/{s}/{role}/postgres`, `ALTER SYSTEM` nel container del database)** |
| mostra tutti | admin, developer | interruttore | Mostra tutti i parametri (N) | «Mostra anche i parametri che si cambiano di rado; quelli già cambiati si vedono sempre» |

### 6.3 Macchina › Monitoraggio, fino a 14 giorni

Pagine: Dashboard `/` e `/links/<n>/`, scheda Monitoraggio (esiste, ultima ora).

- **Periodo**: «Ultima ora · 24 ore · 7 giorni · 14 giorni», bottoni a scelta unica. Default 14 giorni. La scelta resta in questo browser. Con «Ultima ora» la vista è quella di oggi, dal vivo.
- **Quattro tile**, ognuna con il suo confronto:
  - CPU: «media 12% · massimo 100% il 17/11/2026 14:05»;
  - Memoria: «1,4 GB su 1,9 GB adesso · massimo 1,8 GB»;
  - Disco: «libero 41% · pieno fra circa 120 giorni». Il dato viene da `diskhistory.forecast`, che esiste;
  - Rete: «ricevuti 2,1 GB, inviati 8,4 GB nel periodo».
- **Grafici a linee**, uno per misura:
  - CPU, media e massimo, in %;
  - Memoria usata, con la linea del totale;
  - Rete, ricevuti e inviati;
  - Disco libero, giornaliero.
- Un buco (OmniaCLI spento) è una linea interrotta, mai uno zero.
- **Vuoto**: «Lo storico parte dal 21/11/2026 10:00: il periodo di 14 giorni si riempie da solo.»
- **HIDE**: le connessioni al database (stanno nel Monitoraggio dell'istanza); i 14 giorni sull'istanza (l'istanza resta all'ultima ora, come nel riferimento).

**Route da creare**: `GET /api/metrics/history?days=14`. Ogni punto è una media e un massimo su 5 minuti: per 14 giorni sono 4.032 punti per serie, tenuti in un file accanto allo storico dei dischi.

## 7. D — Marketplace: scheda «Moduli» (D1)

### 7.1 Da dove viene il catalogo, senza servizi a pagamento

1. **I repository dell'OCA**:
   - raccomandato: l'elenco del repository di configurazione dell'OCA (`OCA/repo-maintainer-conf`);
   - alternativa: l'API pubblica di GitHub `GET /orgs/OCA/repos`. Sono circa 250 repository in 3 pagine: stanno nel limite di 60 richieste l'ora senza token, o si usa il token git del workspace.
2. **Per ogni repository**, il branch della versione (`19.0`):
   - prima `git ls-remote`: se il commit non è cambiato dall'ultima lettura, non si scarica niente;
   - se è cambiato, un clone parziale e sparso: `--depth 1 --filter=blob:none` con lo sparse checkout dei soli `*/__manifest__.py` e `requirements.txt`. Pochi MB per tutto l'OCA;
   - nessun limite di API.
3. **I manifest si leggono con `ast.literal_eval`, mai eseguiti.** Si prendono `name`, `summary`, `version`, `license`, `author`, `depends`, `external_dependencies`, `development_status`, `installable` (i non installabili non entrano).
4. **Frequenza**: un job ogni notte per le versioni dei template di questa macchina, più «Rileggi il catalogo». Il risultato è un JSON nella cartella di stato.
5. **Niente scheda «Odoo»**. I moduli di Odoo Community sono già nell'immagine e li elenca il menu App di Odoo: sarebbe un doppione. Quelli Enterprise vogliono l'abbonamento e il loro repository. Niente autori terzi in questa prima versione (D4).

### 7.2 Cosa significa «installare»

1. **Il repository del modulo, e quelli dei moduli da cui dipende**, si aggiungono all'istanza con `/api/workspaces/{w}/repositories/add` (esiste): un clone in produzione e in test sul branch `19.0`, l'odoo.conf, i pacchetti Python del `requirements.txt` e il riavvio di **entrambi**. Le dipendenze si risolvono dal catalogo, in modo transitivo.
2. **Il modulo si installa nel database dell'ambiente scelto**: `odoo -d <db> -i <moduli> --stop-after-init` in un container a sé, poi il riavvio. Per la produzione, prima un backup. **(route da creare: `POST /api/servers/{s}/{role}/modules/install`; l'helper `init` lo fa già per un database nuovo, da provare su uno esistente)**
3. **Dove**: in Test per default. In produzione serve un admin (D3).

### 7.3 La scheda

Le schede dell'istanza diventano: **Ambienti · Backup · Log · Monitoraggio · Moduli · Impostazioni**. In Moduli vanno la sezione «Codice» e «Aggiornamenti del codice», che oggi stanno in Impostazioni.

- **Sopra la piega**: la tabella dei repository di UX-006 §4.5 A, invariata. In testa alla scheda il primario **+ Aggiungi moduli**.
- **Dialogo «Aggiungi moduli»** (largo, 960 px):
  - riga: «Catalogo dell'OCA per Odoo 19, letto il 21/11/2026 03:10 · 2.143 moduli.» e «Rileggi il catalogo» (secondario `.small`, admin);
  - ricerca, con il fuoco già dentro. Il `placeholder` è «Cerca per nome o per cosa fa: report excel, partita IVA, magazzino…»;
  - risultati in righe, non schede, al più 50: il nome del modulo in grassetto e il riassunto; in `.small mono` il nome tecnico, il repository e la licenza. Lo stato a parole è uno fra «Installato in Test», «Installato», «Nel codice, non installato», «Da aggiungere»; più «sperimentale» se `development_status` è Alpha. A destra «Installa…» `.small`;
  - **Vuoto senza ricerca**: solo la frase del `placeholder`, nessun elenco di 2.000 righe. **Nessun risultato**: «Nessun modulo dell'OCA per Odoo 19 con "<testo>". Un modulo di un altro repository si aggiunge con + Aggiungi un repository.»
- **«Installa…»** porta il dialogo al passo 2, senza un secondo dialogo sopra. Mostra il riepilogo, con i numeri:
  - «Per installare **Rapporti finanziari** servono: 3 repository nuovi (account-financial-reporting, reporting-engine, server-ux), aggiunti a produzione e test: **si riavviano tutte e due**; 4 moduli; il pacchetto Python xlsxwriter.»;
  - **Dove**: ● Test ○ Produzione. Produzione solo per l'admin, con la riga «prima un backup»;
  - **Installa in Test** (primario del dialogo; l'etichetta segue la scelta) · ‹ Indietro.
- **Prevenzione**: un modulo che dipende da un modulo che l'istanza non ha e il catalogo non conosce (tipico: un modulo Enterprise) ha «Installa…» disattivato. Il motivo sta nel `title` dello `span`: «Richiede account_accountant, che questa istanza non ha (Odoo Enterprise).»
- **HIDE**: la scheda per autore; le schede del catalogo; il caricamento di un modulo .zip (non chiesto il 21/11).

| Pulsante | Chi | Tipo | Etichetta | `title` |
|---|---|---|---|---|
| aggiungi moduli | admin del workspace, developer (D3) | **primario** di Moduli | + Aggiungi moduli | «Cerca un modulo nel catalogo dell'OCA per questa versione di Odoo e lo installa, con i repository da cui dipende» |
| installa… | idem | secondario `.small` | Installa… | «Mostra cosa serve per installarlo (repository, altri moduli, pacchetti Python) e dove; non fa ancora niente» |
| installa | idem | primario del dialogo | Installa in Test / Installa in produzione | «Aggiunge i repository che mancano a produzione e test (si riavviano: chi è collegato perde la connessione per qualche secondo), poi installa i moduli nel database scelto. In produzione fa prima un backup. È un job.» (`/repositories/add` più **la route da creare** `/modules/install`) |
| rileggi catalogo | admin | secondario `.small` | Rileggi il catalogo | «Rilegge dai repository dell'OCA i moduli di questa versione di Odoo. Non installa niente. È un job.» **(route da creare: `POST /api/catalog/refresh`)** |

## 8. Il necessario

- **Stato del sistema**:
  - colonna Backup e conteggi di Istanze;
  - «stanotte: 31 riusciti, 1 non riuscito» di Backup;
  - stato DNS di ogni nome;
  - riga di stato del database e della posta;
  - data del catalogo;
  - ogni azione che è un job apre la fascia del job o la sua pagina (UX-005).
- **Annulla**: c'è una conferma su ogni azione che non si annulla: Elimina backup, Ricostruisci, Svuota la posta, Applica e riavvia il database, Installa in produzione. Un dialogo si chiude con Annulla o Esc senza richieste POST. Nel dialogo dei moduli, «‹ Indietro» torna alla ricerca.
- **Prevenzione dell'errore**:
  - solo server compatibili nella Nuova staging, con i motivi degli esclusi;
  - stima della dimensione prima della copia;
  - un backup che non si è ripristinato alla prova non si usa;
  - un nome già usato è rosso prima del clic;
  - un modulo con dipendenze mancanti non si installa;
  - mai una copia non neutralizzata (regola 4);
  - i parametri del ripristino a un minuto sono fuori dalla regolazione.
- **Messaggio d'errore**: ogni `refuse()` nuovo nasce con la sua chiave italiana. Esempi in §5.1, più:
  - «Il catalogo non si legge da github.com (<motivo>): resta quello del <data>.»;
  - «Il modulo x non si è installato: <prima riga dell'errore di Odoo>. Il log completo è nel job.».
- **Aiuto**: il `title` di ogni pulsante nelle tabelle; gli stati vuoti dicono il primo passo; il `placeholder` della ricerca dei moduli.

## 9. Route da creare (riepilogo)

| Route | Effetto |
|---|---|
| `GET /api/backups` | Tutti i backup di questa macchina, con `kind`, prova di ripristino e automatici falliti (dalla storia dello scheduler). L'orchestratore lo chiede anche a ogni collegamento |
| `GET /api/servers`, campo `backup_schedule` | Per ogni produzione: programmato sì o no, ultimo backup, esito |
| `POST /api/servers/{s}/stagings/from-backup` | Una staging della produzione dal file scelto, neutralizzata |
| `POST /api/servers/{s}/stagings` con `machine` | Staging su un altro server attraverso il collegamento; il record là sa che la produzione è su un'altra macchina |
| `POST /api/servers/{s}/stagings/refresh` con `code` | Anche i repository ai commit della produzione |
| `GET …/mail`, `GET …/mail/{id}`, `POST …/mail/clear` | La posta catturata della staging |
| `/web/save` con `aliases` e `redirects`; `POST /web/check-names` | Altri nomi, reindirizzamenti 301, stato DNS |
| `GET/POST /api/servers/{s}/{role}/postgres` | Parametri, proposta sulla memoria, applicazione |
| `GET /api/metrics/history?days=N` | Medie e massimi su 5 minuti, 14 giorni |
| `GET /api/catalog`, `POST /api/catalog/refresh` | Catalogo OCA per versione |
| `GET/POST /api/servers/{s}/{role}/modules[/install]` | Moduli installati; installazione |

## 10. Aggiornamento di UX-006 §3, regola 1 (pagina dell'istanza)

> 1. **Testata neutra; in ogni scheda al più un `.primary`, ed è l'azione che crea di quella scheda** (Carlo, 21/11/2026; sostituisce D1 e la regola «un solo primario, quello della testata»).
>    - **Testata**, tutti secondari: Apri Odoo ↗ · Entra come… · Avvia (solo da ferma) · Aggiorna · Riavvia (ruolo `user`) · Porta in produzione… (pericolo, solo sul test) · Altro ▾. Eliminare resta in Altro › Pericolo.
>    - **Ambienti**: **+ Nuova staging** (admin del workspace, istanza in un workspace). Pagina di una staging o del test (Panoramica): nessun primario; Ricostruisci… è un secondario di pericolo nella nota in testa.
>    - **Backup**: **Backup adesso** (developer+). «Nuova staging a questo minuto», «Copia completa adesso» e «Salva» dei backup automatici sono secondari.
>    - **Log**: nessun primario (non crea niente). Pausa e Espandi sono secondari.
>    - **Monitoraggio**: nessun primario. Rileggi e Ferma… sono secondari.
>    - **Moduli** (se D1 è adottata): **+ Aggiungi moduli**. «Aggiungi» di un repository, dentro «+ Aggiungi un repository ▸», è secondario.
>    - **Impostazioni**: **Salva e riavvia** (admin), disattivato finché non cambi un'opzione di Odoo o del database, con il conteggio accanto. Senza D1, il primario di Impostazioni è **+ Aggiungi moduli** e «Salva e riavvia» torna secondario.
>    - Con un job in corso il primario della scheda resta al suo posto ma `disabled`, con il motivo sullo `span`: «Su <istanza> lavora un job: riprova quando finisce.»
>    - Il pulsante di conferma di un dialogo è il primario del dialogo: ≤ 1 per `dialog[open]`.
>
> Criterio 2 di UX-006 §6, nuovo: per ogni scheda aperta, `.primary:visible` fuori da `dialog[open]` vale 1 in Ambienti (con la staging possibile), Backup, Moduli e Impostazioni, e 0 in Log e Monitoraggio. Con `.job-band`: `.primary:not([disabled])` = 0. Nella testata: `.primary` = 0.
> Criterio 1 di UX-006 §6, nuovo: schede ≤ 6, e ognuna ha uno dei nomi elencati qui.

## 11. Criteri di accettazione (Playwright)

Come **admin della macchina**, **admin di un workspace**, **developer** e **user** (mai il developer per le schermate del supporto). Viewport 1440×900, lingua IT; Istanze e Monitoraggio anche a 375×812.

Dati:
- 2 collegamenti, uno dei quali spento;
- almeno 3 produzioni, una senza backup automatici e una con l'ultimo automatico fallito;
- 1 staging con 3 messaggi catturati;
- almeno 30 backup.

1. **Istanze**:
   - il conteggio «N senza backup programmato» è uguale alle righe con «Nessun backup programmato» (≥ 1), e il filtro dà esattamente quel numero di righe;
   - la macchina spenta compare come una riga «… non risponde»;
   - la pagina è completa in < 2 s con il collegamento spento (il suo tempo massimo non blocca le altre righe);
   - nessuna riga «Test»; `button` nelle righe = 0;
   - `.primary:visible` = 1 per l'admin della macchina e 0 per user e developer.
2. **Backup**:
   - le righe sono ordinate per data decrescente;
   - un automatico fallito compare come «Non riuscito» con un link a `/jobs/`;
   - nel menu Azioni non c'è nessuna voce che contenga «sopra» o «sostituisce»;
   - con un backup «✗» il primario del dialogo è `disabled` e ha uno `span[title]`;
   - `.primary:visible` fuori da `dialog[open]` = 0.
3. **Nuova staging**:
   - il clic apre un `dialog[open]` e non fa nessuna POST;
   - i server elencati hanno tutti lo stesso template della produzione, e la riga «Non elencati» nomina il collegamento spento;
   - come admin del workspace il campo Server non c'è.
4. **Ricostruisci**:
   - il dialogo ha 2 radio, e «Solo i dati» è già scelto;
   - Annulla non fa nessuna POST;
   - con «Dati e codice», dopo il job, i commit dei repository della staging sono uguali a quelli della produzione.
5. **Posta**:
   - dopo l'invio di 1 e-mail dalla staging (per esempio un preventivo), entro 15 s la tabella ha 1 riga in più, con il destinatario giusto;
   - la casella esterna non riceve niente;
   - l'`iframe` del messaggio ha `sandbox` senza `allow-scripts`.
6. **Indirizzi**:
   - nel dialogo nessun `input[type=checkbox]` dice «HTTPS»;
   - un nome già usato da un altro sito diventa rosso prima del clic;
   - dopo «Salva e pubblica», `curl -I http://cliente.test` dà 301 verso `https://www.cliente.test/` (sulla copia, con un DNS locale).
7. **Database**:
   - la riga di stato contiene una delle due parole «Valori di base» o «Regolato»;
   - il dialogo mostra la base del calcolo con un numero di GB;
   - nessuna riga visibile contiene `archive_mode`, `archive_command` o `wal_level`;
   - dopo «Applica», `SHOW shared_buffers` nel container è il valore proposto.
8. **Monitoraggio**:
   - con «14 giorni» ogni tile ha un valore e un confronto («massimo …» o «pieno fra …»);
   - un buco di 1 ora nei dati non diventa uno zero;
   - la risposta `/api/metrics/history?days=14` arriva in < 1 s.
9. **Moduli**:
   - senza ricerca la lista ha 0 righe;
   - con «excel» compare almeno un modulo di reporting-engine;
   - il riepilogo di «Installa…» conta i repository e i moduli, e dice «si riavviano tutte e due»;
   - un developer vede «Produzione» `disabled`;
   - dopo il job il modulo risulta «Installato in Test», e in produzione non è installato.
10. **Comuni**:
    - ogni `button`, `a.button`, `summary` e `[role=tab]` visibile ha un `title` non vuoto e diverso dal testo;
    - nessuna data `/\d{4}-\d{2}-\d{2}/` a schermo fuori dal Log;
    - nessuna parola inglese fuori da `.mono.small`, dal Log e dal corpo dei messaggi;
    - ogni conferma di §8 compare con il nome dell'oggetto, e annullando non parte nessuna POST.

## 12. Ordine di implementazione (per impatto)

1. **Elenchi globali Istanze e Backup** (A), con l'avviso «nessun backup programmato» e il campo `backup_schedule`. Una produzione senza backup è il danno più caro, e oggi si vede solo aprendo le istanze una per una. Criteri 1, 2.
2. **Ripristina in un'istanza nuova** (staging da un backup): una route piccola su `new_staging`. Criterio 2.
3. **Staging**: dialogo con il server, Ricostruisci, Posta catturata (B). La posta è la parte con più valore per chi prova sulle staging dei clienti. Criteri 3, 4, 5.
4. **Indirizzi** (C1): www e reindirizzamenti sono richieste frequenti dei clienti. Criterio 6.
5. **Monitoraggio di 14 giorni** (C3): serve uno storico nuovo, e prima parte prima si riempie. Si può avviare la sola raccolta già al passo 1. Criterio 8.
6. **Database per istanza** (C2). Criterio 7.
7. **Marketplace** (D): è il lavoro più grande (catalogo, dipendenze, installazione). Criterio 9.

## 13. Decisioni di Carlo

1. **D1 — Dove sta il Marketplace.**
   - **Raccomandata: una sesta scheda «Moduli»**, con dentro Codice, Aggiornamenti del codice e il catalogo, e il primario «+ Aggiungi moduli». «Che moduli ha e ne aggiungo uno» è un lavoro solo, e Impostazioni resta «come gira» (opzioni di Odoo e database), con il primario «Salva e riavvia».
   - Alternativa: 5 schede. Il catalogo è un dialogo che si apre da Impostazioni › Codice, e «Salva e riavvia» torna secondario. Una scheda in meno, ma Impostazioni ha due lavori.
2. **D2 — PostgreSQL: per istanza o per macchina.**
   - **Raccomandata: per istanza**, regolato sulla memoria che il suo workspace gli concede. Ogni ambiente ha il suo container `postgres:17`. Una macchina da 8 GB con 3 istanze ha 6 database: una regolazione «per macchina» darebbe a ciascuno 2 GB di memoria condivisa, 12 GB in tutto su 8 GB di memoria, e la macchina andrebbe in swap.
   - Alternativa: per macchina, come nel riferimento. Serve solo alle istanze fuori dai workspace che usano il PostgreSQL della macchina; per le altre la pagina non regolerebbe niente.
3. **D3 — Dove installa il Marketplace.**
   - **Raccomandata: in Test per default; in produzione solo un admin, con un backup prima.** Esempio, «Rapporti finanziari»: 3 repository aggiunti a produzione e test (si riavviano tutti e due) e 4 moduli installati nel database di Test. La produzione ha il codice ma non il modulo, finché un admin sceglie «Installa in produzione».
   - Alternativa: si sceglie ogni volta, senza default; il developer installa anche in produzione. Un clic in meno, ma un modulo provato da nessuno entra nei dati dei clienti.
4. **D4 — Il catalogo.**
   - **Raccomandata: solo l'OCA**, letto dai repository con git: nessun servizio a pagamento, licenze AGPL/LGPL.
   - Alternativa: anche i repository pubblici di altri autori, da un elenco tenuto a mano. Più moduli, ma la qualità e la licenza vanno controllate per ognuno.
5. **D5 — Dove si cambiano gli indirizzi.**
   - **Raccomandata: nel dialogo «Indirizzi…» dell'ambiente.** Workspace › Nomi sul web elenca tutti i nomi in sola lettura, con «Indirizzi… ›» per ogni istanza, e tiene solo dominio, istanza principale e certificato. I sottodomini di oggi passano nel dialogo.
   - Alternativa: tutto resta in Nomi sul web, con due tabelle nuove (altri nomi, reindirizzamenti). Un posto solo, lontano dall'istanza.
6. **D6 — «Ripristina in un'istanza nuova».**
   - **Raccomandata: diventa una staging della sua produzione**, neutralizzata. Esempio: il backup di essetech.it del 18/11/2026 03:00 diventa stg-20261121.essetech.it, accanto alla produzione, senza posta e senza azioni pianificate. Serve una route piccola.
   - Alternativa: un'istanza nuova a sé, su un server e in un workspace a scelta, con un nome libero. Serve un'istanza da backup senza archivio completo, quindi anche il template e i repository vanno scelti a mano.
7. **D7 — La posta della staging.**
   - **Raccomandata: letta nella scheda Posta**, attraverso OmniaCLI: stesso login, nessuna porta né nome pubblico nuovi.
   - Alternativa: un pulsante che apre la pagina web del servizio che cattura la posta. Meno lavoro, ma vuole un nome, un certificato e un login suoi.
8. **D8 — Il menu.**
   - **Raccomandata:** per l'admin della macchina, «Istanze» apre l'elenco globale e c'è la voce «Backup». La Dashboard di questa macchina si apre dal logo e da Server.
   - Alternativa: «Istanze» resta la Dashboard, e l'elenco globale è un link «Tutte le macchine ›» in testa.
9. **D9 — Ricostruisci.**
   - **Raccomandata: un pulsante con la scelta nel dialogo** (solo i dati, già scelto; oppure dati e codice).
   - Alternativa: due pulsanti, «Ricarica i dati…» e «Ricostruisci…». Più espliciti, ma due pulsanti simili uno accanto all'altro (Hick).

**Fuori da questo contratto** (visti il 21/11, non chiesti): frequenze di backup fino a ogni ora (lo scheduler accetta già qualunque cron, oggi il modulo chiede un orario al giorno); caricamento di un modulo .zip; aggiunta di un server dalle API dei fornitori cloud; 2FA e ultimo accesso degli utenti.

## Non verificato

- Che `odoo neutralize` spenga i server di posta del database in **ogni** versione dei template (18, 19), così che Odoo usi lo `smtp_server` dell'odoo.conf: dedotto, non provato. Lo stesso per il comportamento del test di una staging.
- Che l'helper `init` (`odoo -i … --stop-after-init`) funzioni su un database esistente: l'uso documentato è per un database nuovo.
- Il percorso e il formato dell'elenco dei repository in `OCA/repo-maintainer-conf`: non letto. L'API `GET /orgs/OCA/repos` è l'alternativa sicura.
- Che il proxy `/links/<n>/…` porti le POST di `/api/instances/new` e di `/stagings` con il ruolo admin del collegamento: la route proxy accetta POST (`app.py` riga 4777), il ruolo trasmesso non l'ho letto.
- Il tempo di Istanze e Backup con collegamenti veri: la soglia di 2 s è una richiesta, non una misura.
- Il campo `memory_gb` del workspace come base della regolazione: esiste fra i limiti (`WORKSPACE_LIMITS`), ma non ho letto come l'helper `limits` lo applica allo slice.
- Le schermate del riferimento sono quelle di un amministratore. Nessuna misura di OmniaCLI è stata presa per questo contratto: niente Playwright, solo codice letto alla revisione 1dccd57.

## Tracciabilità

Visita dal vivo del pannello di riferimento con Carlo, 21/11/2026 (note `confronto-pannello-21-11.md`, schermate 01-28); decisioni di Carlo dello stesso giorno (regola del primario; costruire A, B, C, D). Estende UX-001…005 (`UX-001-hosting-panel-parity.md`), UX-006 (§3 regola 1 sostituita, §10), UX-007 (Nomi sul web, D5). Nessun ticket `T`/`HT` collegato.

## Decisioni prese

Il 21/11/2026 si costruisce con le raccomandate (D1-D9) finché Carlo non dice altro; D2 l'ha decisa Carlo il 21/11/2026,
**contro la raccomandata**: «Farei un Postgresql per Macchina ma attenzione che la singola istanza potrebbe far uso di
un Postgresql su un altra macchina. Poi va considerato il caso di Odoo19 che può montare 2 database in cluster».
§6.2 (Impostazioni › Database per istanza) non si costruisce così: la forma la decide un ADR (topologia per macchina,
database remoto, due database in cluster, migrazione), e §6.2 si riscrive dopo.
