# UX-011 — Dopo la promozione, e le altre repliche

Stato: contratto di design, prima della build. È un'aggiunta a UX-010, e ne valgono tutte le regole (§2), gli stati (§3), la matrice (§4) e la redazione per il cliente. Le classi e i dialoghi sono gli stessi: `omniaAsk`, `omniaTypeName`, `span[title]` per i rifiuti prima del clic, nessun dialogo del browser.
P = la macchina che aveva il cluster ed era il primario (lab2, 172.30.0.12). R = la macchina che ospita la copia (lab1, 172.30.0.11). Tutto è dell'admin della macchina, salvo dove è detto.
Origine: il «Non costruito» di AD-004, opzione A, dopo il rilascio 2026.1.5469 (22/11/2026).

## 0. Regole che UX-011 aggiunge
1. **Un nome solo per tornare indietro: «il ritorno»** (EN «the return»). A schermo mai «failback», «failover», «standby», «slot», «WAL», «timeline».
2. **Chi ospita la copia la chiede**, attraverso il suo collegamento verso chi ha i dati (come in UX-010). Nel ritorno la copia sta su P, quindi serve un collegamento da P a R.
3. **Un'istanza si sposta da sola solo se è già ferma.** Mai si riavvia da sola un Odoo che sta rispondendo (§B).
4. Ogni azione è un job, nel log degli accessi di tutte e due le macchine, con l'utente e il collegamento. Uno spostamento automatico ha come utente «automatico: database spostato da lab2».

## A. Il ritorno dopo una promozione

**Lavoro primario**: riportare su P il database delle sue istanze senza perdere niente, decidendo il momento del fermo. Il percorso ha due passi: Prepara (niente si ferma) e Completa (circa 10 s per istanza).

**Dove si entra**: nel dettaglio della riga sostituita di P, dentro `.pg-actions`, «Prepara il ritorno…» (`.small`, non pericolo) accanto a «Togli il cluster…». Il `title` dello stato «Sostituito…» sulla riga chiusa termina con «Apri la riga per riportarlo qui.» La scheda continua a non avere primari.

| Pulsante | EN | IT | `title` IT |
|---|---|---|---|
| entrata (P) | Prepare the return… | Prepara il ritorno… | Copia qui, attraverso il collegamento, il database che oggi è su lab1, e lo tiene al passo in sola lettura. Le istanze restano su lab1: non si ferma niente. È un job. |
| conferma | Prepare the return | Prepara il ritorno | Mette da parte i dati di questo cluster e copia qui quelli di lab1 |
| fine (P) | Complete the return… | Completa il ritorno… | Ferma la copia su lab1, aspetta che qui sia arrivata ogni modifica, poi riporta le istanze su questo cluster e le riavvia. È un job. |
| annulla (P e R) | Cancel the return… | Annulla il ritorno… | Cancella la copia fatta qui e rimette al loro posto i dati di prima; lab1 toglie quello che tiene per lei e ne libera il disco. Le istanze restano su lab1. È un job. |
| dopo (R) | Make the replica again… | Rifai la replica… | Cancella questa copia ferma e ne fa una nuova del cluster di lab2, tenuta al passo. È un job. |

**Stati sulla riga chiusa di P** (colonna Regolazione, dove oggi sta «Sostituito…»):
- blu: «Ritorno da lab1: copia in corso, 64 % · 1,6 GB di 2,6 GB» / «Return from lab1: copying, 64 % · 1.6 GB of 2.6 GB»;
- verde: «Ritorno da lab1: pronto · in pari 2 s» / «Return from lab1: ready · in sync 2 s»;
- ambra: «Ritorno da lab1: in ritardo di 45 s» / «Return from lab1: behind by 45 s»;
- rosso: «Ritorno da lab1: non riceve dal 23/11/2026 09:40: <motivo>» / «Return from lab1: not receiving since …: <reason>»;
- dopo il ritorno: torna lo stato normale della regolazione, più la `.pg-sub` «Tornato da lab1 il 23/11/2026 09:12: di nuovo il database delle 5 istanze» / «Back from lab1 on …: again the database of the 5 instances». La riga resta finché esiste la copia ferma su R.

**Dettaglio di P durante il ritorno**: un `dt` «Ritorno», al posto del paragrafo «I dati di questo cluster sono del…».
- «Da lab1 (172.30.0.11), in TLS · iniziato il 23/11/2026 09:02 · pronto · in pari 2 s»
- «Messi da parte: i dati del 22/11/2026 06:16 (2,5 GB). Si cancellano quando il ritorno è completo, e tornano al loro posto se lo annulli.»
- «Le 5 istanze lavorano ancora su lab1.»
- Azioni: «Completa il ritorno…» (pericolo; `disabled` finché il ritorno non è stato in pari, con lo `span[title]` «Il ritorno non è ancora una copia intera: si completa solo dopo che è stato in pari.») e «Annulla il ritorno…» (pericolo).

**Riga di R nel frattempo**: lo stato resta «Promossa il …». Si aggiunge la `.pg-sub` «Ritorno su lab2: copia in corso, 64 %», «… in pari · 2 s», ecc. (le parole `pSync`, `pBehind`, `pOff` di UX-010), con il badge «tiene 1,2 GB» sotto la stessa regola, e il tetto di 20 GB. Il dettaglio ha un `dt` «Ritorno su lab2» con «Annulla il ritorno…».
**Riga di R dopo il ritorno**: grigio, «Tornata a lab2 il 23/11/2026 09:12: ferma, i dati sono di quel momento» / «Returned to lab2 on …: stopped, its data is of that moment». Azioni: «Rifai la replica…» ed «Elimina la replica…». La copia ferma non riparte da sola, nemmeno quando R si riavvia.

**Dialogo «Prepara il ritorno…»** (`omniaAsk`):
«Riportare su questa macchina il database delle 5 istanze, oggi su lab1?
1. I dati di questo cluster, fermi dal 22/11/2026 06:16 (2,5 GB), si mettono da parte: non si cancellano finché il ritorno non è completo.
2. Da lab1 si copia qui tutto il suo database: 2,6 GB, in circa 1 minuto. Qui ci sono 288 GB liberi, e ne restano almeno 2 GB.
3. Questo cluster segue lab1 a ogni modifica, in sola lettura, finché non scegli Completa il ritorno….
Le 5 istanze continuano a lavorare su lab1: non si ferma niente. Su lab1 nascono un ruolo, una riga di accesso per 172.30.0.12 sola e un punto che tiene le sue modifiche, al massimo 20 GB. Andata e ritorno fra le due macchine: 0,4 ms.»
EN: «Bring back to this machine the database of the 5 instances, on lab1 today? 1. The data of this cluster, stopped since …, is set aside: it is not deleted until the return is complete. 2. The whole database of lab1 is copied here: 2.6 GB, in about 1 minute. … 3. This cluster follows lab1 at each change, read only, until you choose Complete the return…. The 5 instances keep working on lab1: nothing stops. …»
Se la copia su R deve rifarsi per lasciar partire il ritorno, compare la riga in ambra «La copia su lab1 si rifà, in circa un secondo: le 5 istanze perdono le connessioni.». In quel caso la conferma diventa «Prepara il ritorno e riavvia la copia su lab1», e c'è una seconda conferma, come in UX-010 §5.2.

**Dialogo «Completa il ritorno…»** (`omniaTypeName`; si scrive «lab1», la macchina la cui copia si ferma):
«Completare il ritorno da lab1?
1. La copia su lab1 si ferma: da quel momento le 5 istanze non scrivono.
2. Questo cluster riceve le ultime modifiche (ritardo adesso: 2 s) e accetta le scritture. Non si perde niente: se in 60 s non è arrivato tutto, la copia su lab1 riparte e le istanze restano là.
3. Le 5 istanze (acme-mach, …) tornano su questo cluster e si riavviano una dopo l'altra, prima le produzioni: circa 10 s ciascuna, 50 s per l'ultima.
4. I dati messi da parte (del 22/11/2026 06:16, 2,5 GB) si cancellano.
Il database che un'altra macchina usa (acme_linked, di lab1) torna qui: la sua istanza lo segue da sola entro un minuto (§B).
Dopo, la copia su lab1 è ferma, con i dati di quel momento: da lì si rifà come replica o si elimina. Scrivi lab1 per confermare.»

**Rifiuti** (prima del clic, nello `span[title]`):
- Senza collegamento: «Serve un collegamento da questa macchina a lab1, in HTTPS o SSH: si aggiunge da Server › + Aggiungi un server.»
- Collegamento in HTTP semplice: la frase di UX-010.
- Prepara, R che non risponde: «lab1 non risponde: <motivo>. Il ritorno si prepara attraverso il collegamento: riprova quando risponde.»
- Completa, R che non risponde: «lab1 non risponde: il ritorno ferma prima la sua copia, e senza la sua risposta le due copie divergerebbero. Riprova quando risponde.» Il ritorno non ha una strada «spento»: è pianificato.
- Spazio: «Non c'è spazio: la copia da lab1 prende circa 2,6 GB, i dati messi da parte restano (2,5 GB), qui ci sono 3,1 GB liberi e 2 GB devono restare liberi.»

**«Togli il cluster…»** resta l'altra strada, quella per non tornare. È `disabled` durante un ritorno, con «C'è un ritorno in corso: annullalo prima.», e il suo dialogo nomina le concessioni (Parte 1, punto 2). Dopo il ritorno vale la regola normale.

**HIDE**: dove stanno i dati messi da parte (la parola è «messi da parte»), la linea temporale e le posizioni, il ruolo e il punto di ritenuta su R, i passi interni del completamento (sono nel job).

**Visibilità**: admin di P, tutto su P; admin di R, la sua riga; cliente, niente durante il ritorno. A ritorno completato le istanze di P tornano a «cluster della macchina · valori di base», come prima della promozione.

**Criteri di accettazione** (dopo una promozione; lab2 = P, lab1 = R; 1440×900; IT):
1. Su lab2 senza collegamento verso lab1: «Prepara il ritorno…» è `disabled`, lo `span[title]` contiene «Aggiungi un server», le POST sono 0.
2. Con il collegamento: il dialogo contiene due dimensioni `/\d+(,\d+)? (MB|GB)/`, «non si ferma niente» e «ms». Annulla fa 0 POST.
3. Durante il job la riga chiusa di lab2 dice «Ritorno da lab1: copia in corso» senza aprire la riga, e quella di lab1 ha «Ritorno su lab2:». Poi lab2 dice «pronto · in pari», e le 5 istanze rispondono 200 con `db_host` verso lab1.
4. Un valore scritto su acme-mach (che è su lab1) si legge sul cluster di lab2 entro 10 s.
5. «Completa il ritorno…» è `disabled` prima del primo «in pari», e il dialogo vuole «lab1».
   - Dopo il job: un valore scritto 1 s prima del clic c'è su lab2, la copia di lab1 è ferma, e le 5 istanze rispondono 200 entro 60 s sul socket.
   - lab2 ha «Tornato da lab1», lab1 ha «Tornata a lab2» e «Rifai la replica…».
   - Dopo un riavvio del servizio OmniaCLI di lab1, la copia è ancora ferma.
6. «Annulla il ritorno…» quando il ritorno è pronto: un valore scritto prima della promozione si rilegge su lab2, lo stato torna «Sostituito…» e il badge «tiene» di lab1 sparisce.
7. Con il pannello di lab1 fermo, «Completa il ritorno…» rifiuta con «lab1 non risponde» e nessuna istanza si riavvia.
8. Prepara, Completa, Annulla e Rifai compaiono nel log degli accessi di tutte e due le macchine. Nel DOM ci sono 0 occorrenze di «failback», «standby», «slot», «WAL» e «timeline».

## B. I database concessi ad altre macchine, dopo una promozione

**Scelta: automatico**, sotto la regola 3 del §0. L'istanza di A si sposta da sola quando sono vere tutte e tre le cose:
- il suo database non risponde da 60 s;
- P, attraverso il collegamento di A, dice «sostituito, ora su R», con l'indirizzo e la CA della copia;
- la nuova destinazione risponde con il database e il ruolo, provata come la proverà Odoo.

Se una delle tre manca, l'istanza non si sposta e la pagina lo dice.
- **Perché**: dopo una promozione l'istanza di A è già senza database, quindi il riavvio non costa niente a chi lavora.
- **Il caso in numeri**: acme_linked su lab1, promozione alle 06:16. In automatico torna verso le 06:17 (un giro di 20 s, la prova, 10 s di riavvio). Con un pulsante resta ferma finché l'admin di A non apre la pagina: un sabato notte sono ore.
- **Al ritorno (§A)** vale lo stesso: quando la copia su R si ferma, l'istanza di A è ferma e segue il database.
- **Promozione con P spento**: A lo sa solo quando il pannello di P risponde di nuovo. Fino ad allora l'istanza non ha database, e il dialogo di R lo dice.

**La copia promossa su R** lascia entrare gli stessi ruoli dagli stessi indirizzi (riga di accesso e firewall), dal momento della promozione. Nel dettaglio della riga di R promossa compare il `dt` «Da altre macchine», con le righe e le azioni del cluster (UX-008), perché adesso quelle concessioni vivono lì.

**Il dialogo «Promuovi…» su R**: la frase `promoteLent` diventa
- P risponde: «I database che altre macchine usano su quel cluster (acme_linked, per lab1 da 172.30.0.11) sono in questa copia, che li lascia entrare dagli stessi indirizzi. Ogni macchina punta qui la sua istanza da sola, entro un minuto da quando lab2 glielo dice, e la riavvia (circa 10 s).» / EN «The databases other machines use on that cluster (…) are in this copy, which lets them in from the same addresses: each machine points its instance here by itself, within a minute of lab2 telling it, and restarts it (about 10 s).»
- P spento: «… da quando lab2 risponde di nuovo: fino ad allora quelle istanze non hanno database.»

**Riga sostituita di P**: «da altre macchine: acme_linked (lab1) · ora sulla copia di lab1, seguito il 22/11/2026 06:17», oppure «· non ancora seguito».

**Pagina dell'istanza su A, admin della macchina** (riga «Server del database»):
- con P che dice dove: in ambra, «· non raggiungibile dal 22/11/2026 06:16: lab2 lo ha spostato su lab1, Odoo viene puntato là da solo» / «· not reachable since …: lab2 moved it to lab1, Odoo is pointed there by itself»;
- con P che non risponde: in rosso, «· non raggiungibile dal 22/11/2026 06:16: lab2 non risponde (<motivo>)»;
- dopo: «PostgreSQL 17 di lab2, sulla copia promossa su lab1 (172.30.0.11) · dal 22/11/2026 06:17» / «PostgreSQL 17 of lab2, on the promoted copy on lab1 (…) · since …», con il link «Database di lab2 ›»;
- se il database dice spostato ma il vecchio risponde ancora: nessuno spostamento, e in ambra «· lab2 dice che è su lab1, ma qui risponde ancora: non si sposta da solo».

**Riga «Non qui» su A**:
- «Non qui: acme-linked usa PostgreSQL 17 di lab2, non raggiungibile dal 22/11/2026 06:16: <motivo>.» (`.pg-warn`);
- dopo: «Non qui: acme-linked usa il database di lab2 che ora è su lab1 (172.30.0.11:15432), la copia promossa; si regola là.»;
- se A è R (come sulle macchine di prova), niente «Non qui». La riga «Cosa contiene» della copia dice «… e acme_linked, usato da acme-linked di questa macchina».

**Il cliente di A**: con il database irraggiungibile legge «Database: non raggiungibile dal 22/11/2026 06:16»; dopo, «su un'altra macchina · si regola là». Nessun nome di macchina, nessun indirizzo, nessun motivo.

**HIDE**: la CA e il ruolo passati da P ad A; le tre condizioni (sono nel job di A).

**Criteri**:
1. Promozione con lab2 che risponde: acme-linked su lab1 risponde 200 entro 90 s, con `db_host` verso la copia, e nessun clic.
2. Promozione con il pannello di lab2 fermo: la pagina di acme-linked dice «lab2 non risponde». Al ritorno di lab2 l'istanza segue entro 90 s.
3. Come admin del workspace, il DOM e le route di acme-linked contengono 0 volte «lab1», «lab2» e «172.30.».
4. Lo spostamento è nel log di lab1 con l'utente «automatico», e la risposta di lab2 è nel log di lab2.
5. Dopo il ritorno (§A), acme-linked torna su lab2 entro 90 s.

## C. Più di una replica per cluster

**Limite: 2 repliche per cluster.**
- Perché: i lavori sono due, una copia da promuovere e una da cui leggere (possono essere la stessa). Odoo legge da una replica sola per istanza, quindi una terza non aggiunge un lavoro, aggiunge solo rischio.
- Il caso in numeri: P con 41 GB liberi (UX-010). Due repliche sparite tengono al massimo 2 × 20 = 40 GB; con tre sarebbero 60 GB, più del disco.

**Riga chiusa di P**: una `.pg-sub` per replica, al massimo 2, la peggiore per prima (rosso, ambra, blu, verde). Ogni riga ha il suo badge «tiene N GB». Niente conteggio: con 2 righe si leggono tutte e due.
**Dettaglio**: il `dt` diventa «Repliche di lettura», con un blocco per replica (linea, chi legge, tenuto, tetto, «Cambia il tetto…», «Elimina la replica…»), nello stesso ordine.

**Tetto**: 20 GB per replica, come prima. Il dialogo di creazione su R aggiunge «Con questa, lab2 tiene al massimo 40 GB per le sue repliche, su 41 GB liberi.», in ambra sopra il 50 % dei liberi.
- Rifiuto per spazio: «Non c'è spazio su lab2: le repliche potrebbero tenere 40 GB, e lab2 ha 38 GB liberi con 2 GB da lasciare. Abbassa il tetto.»
- Rifiuto per limite (sostituisce quello di UX-010): «Il cluster di lab2 ha già 2 repliche, su lab1 e lab3: il massimo è 2. Eliminane una, da lì o da lab2.»

**«Leggi dalla replica…»**:
- Con una replica, il dialogo è quello di oggi.
- Con due, si aggiunge una select «Da quale replica» / «From which replica». Ogni opzione dice macchina, stato e andata e ritorno: «lab1 (172.30.0.11) · in pari · 2 s · 0,4 ms».
- Le repliche non collegate o da rifare sono opzioni `disabled`, con lo stato nel testo.
- Il default è la replica in pari con l'andata e ritorno più breve. Il resto del testo cita quella scelta.
- «Cambia replica» è DEFER: si fa con Smetti, poi Leggi (2 riavvii di circa 12 s).

**Promozione con un'altra replica presente**: il dialogo aggiunge «La replica su lab3 resta una copia del cluster fermo di lab2: non segue questa. Le istanze che leggono da lab3 (beta-shop) smettono di leggere da una replica.». Dopo la promozione, la riga di lab3 dice in rosso «Il suo primario è stato sostituito dalla replica su lab1 il …: non riceve più», con la sola «Elimina la replica…». La nuova replica si fa da lab3 verso lab1.

**Criteri**:
1. Con 2 repliche, la riga chiusa di lab2 ha 2 `.pg-replica-line`, e la prima è quella con lo stato peggiore.
2. Una terza creazione è `disabled`, con «il massimo è 2» e 0 POST.
3. La select di «Leggi dalla replica…» c'è con 2 repliche e manca con 1. Un'opzione non collegata è `disabled`.
4. Dopo la promozione di lab1, la riga di lab3 dice «sostituito» e beta-shop ha `db_replica_host` vuoto.

## D. La replica di un database dedicato

**Lavoro primario**: la stessa copia pronta a sostituire, ma per un'istanza sola e per un cliente solo.

**Dove si entra su R**: lo stesso «+ Replica di un'altra macchina…».
- Il dialogo ha una seconda scelta, «Di quale PostgreSQL» / «Which PostgreSQL», solo quando P ne offre più di uno: «Il cluster della macchina: 5 istanze, 2,6 GB» e «Quello di acme-odoo (Produzione): 106 MB».
- Si offrono solo le produzioni con PostgreSQL dedicato. Staging e test sono HIDE.
- Una replica per dedicato.

**I fatti, nel dialogo**:
- «La copia è il PostgreSQL di acme-odoo su lab2: 106 MB, i dati di un cliente solo (workspace acme).»
- «Su lab2 le modifiche tenute per la replica stanno sul disco del workspace acme (50 GB, 31 GB liberi): al massimo 5 GB.» Il default è D11-1.
- Sempre la prima volta, in ambra: «Il PostgreSQL di acme-odoo si rifà per aprire una porta in TLS, in circa 5 s: acme-odoo non risponde per quel tempo, e chi sta salvando riceve un errore.» La conferma diventa «Crea la replica e riavvia il PostgreSQL di acme-odoo», con la seconda conferma.

**Riga su R**: «Replica di lettura di acme-odoo (lab2)» / «Read replica of acme-odoo (lab2)», badge «PostgreSQL 17».
- `.pg-sub`: «Copia del PostgreSQL di acme-odoo su lab2 · in TLS sulla porta 15434, solo per lab2».
- Stati come in UX-010 §3.
- «Cosa contiene»: «Il database di acme-odoo, del workspace acme: un cliente solo. Leggibile solo da root e dall'utente della copia.»

**Su P**:
- La riga di acme-odoo nella scheda Database ha la `.pg-sub` «Replica su lab1: in pari · 2 s» con il badge, e nel dettaglio il `dt` «Replica di lettura», con lo stesso blocco del cluster.
- La pagina dell'istanza, per l'admin della macchina: «dedicato · valori di base · legge anche dalla replica su lab1 (172.30.0.11)», oppure «Leggi dalla replica…» (da Odoo 18).

**Promozione: sì, solo per quell'istanza** (`omniaTypeName`, si scrive «acme-odoo»):
«Promuovere la replica di acme-odoo (lab2)?
1. Il PostgreSQL di acme-odoo su lab2 si ferma: acme-odoo non risponde.
2. Questa copia accetta le scritture.
3. acme-odoo viene puntata qui e si riavvia: circa 10 s senza Odoo in tutto.
Ritardo adesso: 2 s, e le modifiche di quei secondi non arrivate si perdono. Dopo, il database di acme-odoo vive su lab1, e i suoi backup passano dalla rete. [solo se acceso] Il ripristino a un minuto di acme-odoo si ferma: l'ultimo punto è del 23/11/2026 09:12. Le sue staging restano su lab2 e non cambiano. Si torna con Prepara il ritorno…, dalla riga di acme-odoo su lab2. Scrivi acme-odoo per confermare.»
- Dopo, la riga di acme-odoo su lab2 è «Sostituito dalla replica su lab1 il …» con «Prepara il ritorno…» (§A, per un'istanza).
- Lo spazio dei dati di prima si libera con «Libera lo spazio di prima…», che è già nella pagina dell'istanza.

**Cliente** (admin del workspace, developer, user):
- «Letture: anche da una replica» quando l'istanza legge (UX-010 D5);
- dopo la promozione, «su un'altra macchina · si regola là»;
- l'esistenza della copia, vedi D11-2;
- nessun pulsante, nessun nome, nessuna dimensione.

**HIDE**: la porta del dedicato, l'override di compose, il ruolo e il punto di ritenuta.

**Criteri**:
1. Il dialogo su lab1 mostra «Di quale PostgreSQL» solo quando lab2 ha una produzione dedicata. Una staging non compare mai.
2. La creazione riavvia il PostgreSQL di acme-odoo una volta: `/web/login` non risponde per 10 s al massimo.
3. Con la replica ferma e 200 MB scritti, il badge «tiene» sulla riga di acme-odoo resta entro ±10 % dello slot, e lo spazio cresce sul disco del workspace acme.
4. Dopo la promozione: acme-odoo risponde 200 entro 30 s con `db_host` verso lab1, e le altre 4 istanze di lab2 non si riavviano (il loro uptime non cambia).
5. Come admin del workspace: 0 volte «lab1», «15434» e «106 MB», nel DOM e nelle route.

## Decisioni per Carlo (la raccomandata per prima)

1. **D11-1 — Le modifiche tenute per la replica di un dedicato stanno sul disco del cliente.**
   - **Raccomandata: tetto del 10 % del disco del workspace, al massimo 20 GB.** Esempio: 5 GB su un workspace da 50 GB. Con una replica sparita e 0,2 GB all'ora, il tetto si raggiunge in circa 25 ore; la replica si rifà (106 MB, pochi secondi) e il disco del cliente non si riempie mai.
   - Alternativa: 20 GB come per il cluster. Su 31 GB liberi ne restano 11 al cliente, fermo per una scelta del fornitore.
   - Resta di Carlo anche se quello spazio conta nello spazio del cliente.
2. **D11-2 — Il cliente legge che esiste una copia dei suoi dati su un'altra macchina (D).**
   - **Raccomandata: sì.** Una riga senza nomi in Dettagli tecnici, «Copia del database: anche su un'altra macchina del fornitore», e il job di creazione e di promozione nel suo log, senza nomi. Dice al cliente dove stanno i suoi dati (NIS 2), anche quando non legge dalla copia.
   - Alternativa: come UX-010 D5, solo quando l'istanza legge dalla copia.
3. **D11-3 — Lo spostamento automatico (§B) vale anche quando A è di un altro proprietario.**
   - **Raccomandata: sì, per tutti.** Sposta solo un'istanza già ferma: per il cliente di A sono circa 30 s contro ore.
   - Alternativa: un interruttore per collegamento, «segue da solo il database», acceso per default. Costa una riga in più nella pagina del collegamento.

## Non verificato
- Se il ritorno chiede di rifare la copia su R (valori del cluster per le copie in uscita): il dialogo lo dice solo se serve, va misurato su lab1.
- I tempi di Completa (10 s per istanza) e di B (90 s) sono stime, dalla promozione del 22/11: vanno misurati.
- La replica di un dedicato con Odoo 17: il pulsante «Leggi…» segue UX-010 §6; la promozione non dipende dalla versione di Odoo.

## Tracciabilità
- Nessun `TS-NNN` né `T<n>`/`HT<n>`: è il «Non costruito» di AD-004 dopo il rilascio 2026.1.5469 (22/11/2026).
- Documenti: `docs/ux/UX-010-replica-di-lettura.md` (§3, §4, §6, «Scelte fatte per costruire», «Dopo la verifica»); `docs/adr/postgresql-della-macchina.md`, AD-004 opzione A, «Costruita» e «Non costruito». AD-003 (collegamento, TLS) e AD-006 (spazio detto prima).
- Codice: `omniacli/templates/_machine_database.html` (righe 336-462, 549-589, 673-909), `omniacli/templates/server.html` (righe 983-1014), `omniacli/app.py` (`database_place`, riga 12401; riga del cluster, riga 10295).
- Il ritorno (§A) e lo spostamento automatico (§B) vanno scritti in AD-004 da `software-architect` prima della build: cambiano il verso delle chiamate fra le macchine.

## Scelte fatte per costruire, in attesa di Carlo (22/11/2026)

Le tre decisioni restano di Carlo. Per costruire valgono le raccomandate, tutte regolabili:

- **D11-1**: per la replica di un dedicato, tetto del 10 % del disco del workspace, al massimo 20 GB.
- **D11-2**: il cliente legge che esiste una copia dei suoi dati su un'altra macchina del fornitore, senza nomi.
- **D11-3**: lo spostamento automatico vale per tutti i collegamenti, e sposta solo un'istanza già ferma.
