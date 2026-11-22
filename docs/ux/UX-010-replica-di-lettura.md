# UX-010 — La replica di lettura del cluster della macchina

Stato: contratto di design, prima della build.
- Lo implementa chi scrive `omniacli/templates/_machine_database.html` e `server.html`, con Jinja e JS senza framework.
- Le classi sono quelle che esistono già: `.pg-state`, `.badge`, `.pg-sub`, `.pg-actions`, `details`, `dialog`.
- Le stringhe vanno in `omniacli/i18n/it/*.json`, con il sorgente inglese in `_()`.

Valgono UX-001…009, in particolare:
- UX-008 §2, regole 1, 2, 5 e 6;
- UX-008 §6.2: scheda Database, `title` scritti dalla route, nessun dialogo del browser, date con `whenText`;
- UX-009 §7, redazione per lo `scoped`.

Origine: AD-004, opzione A («replica in streaming fisica del cluster, promozione manuale con un job»), ultimo passo del §9 dell'ADR. Richiesta del 22/11/2026.

## 0. Cosa c'è oggi (revisione d7c99ee)

- La replica esiste solo **di terzi** (AD-004, costruito il 21/11). L'indirizzo lo dà chi gestisce il database, e OmniaCLI lo scrive in `odoo.conf`.
- La pagina dell'istanza dice «su host · si regola là · replica di lettura su host» (`server.html` riga 994), anche al cliente (UX-009 D3).
- La mappa disegna quella replica come riquadro puntinato «replica · sola lettura» sotto la macchina che la usa (`_map.html` righe 1033-1064).
- Il cluster della macchina non ha nessuna replica. La sua riga ha già Serve, Dove, Ripristino a un minuto e Da altre macchine, ognuna con le sue azioni.

## 1. Persone, posti, lavoro primario

Persona: **l'integratore, admin della macchina** (Carlo), su desktop a 1440×900. Il telefono serve solo a leggere lo stato (375×812). Per i ruoli del cliente vale solo §4, riga «istanza».

Due macchine:
- **P** ha il cluster della macchina, cioè il primario;
- **R** è un altro OmniaCLI, con un collegamento verso P, che ospita la replica.

| Posto | Domanda a cui risponde | Primario |
|---|---|---|
| **R › Database** | «Ho una copia di P pronta a sostituirlo, ed è al passo?» | **+ Replica di un'altra macchina…** quando nella scheda non c'è altro da creare. Se manca ancora il cluster di R, il primario resta «+ Cluster della macchina» e la replica è secondaria |
| **P › Database**, riga del cluster | «La replica c'è, è al passo, e non mi sta riempiendo il disco?» | nessuno (la replica si crea da R) |
| **Istanza di P** › Impostazioni › Dettagli tecnici, riga «Server del database» | «Da dove legge questa istanza, e cosa succede se una delle due cade?» | nessuno |

Il lavoro primario dello schermo è **vedere in un colpo d'occhio se la replica è al passo**, e su P **se trattiene il disco**. Creare la replica è un'azione rara, e promuoverla lo è ancora di più. Leggere dalla replica è una scelta per istanza.

## 2. Le regole che UX-010 aggiunge

1. **La replica ha un nome solo**, per tutte e due le macchine: «Replica di lettura». Su R è «Replica di lettura di lab2», su P «Replica di lettura su lab1 (172.30.0.11)». Mai «standby», «slot», «streaming» o «WAL» a schermo. I nomi tecnici vanno solo dopo l'etichetta, in `.small mono`, e solo dove servono (Odoo 17).
2. **Lo stato sta sulla riga chiusa** (sulla tabella, non nel dettaglio), con il pallino e la parola. Vale per tutti e due i lati. È la lezione del rilievo 1 della Parte 1.
3. **Il failover manuale si dice dove si decide**: nel dialogo «Leggi dalla replica…» e nel dialogo «Promuovi…», non in un manuale.
4. **Ogni azione della replica è un job**, nel log degli accessi di tutte e due le macchine, con l'utente e il collegamento (NIS 2).

## 3. Stati (pallino `.pg-state` + parola; il colore non è mai solo)

| Stato | Quando | Colore | IT (riga su R) | IT (riga del cluster su P) |
|---|---|---|---|---|
| copia in corso | il job di creazione è al passo della copia | blu | «Copia in corso: 64 % · 91 MB di 142 MB» | «Replica su lab1: copia in corso, 64 %» |
| in pari | collegata, ritardo ≤ 10 s (D2) | verde | «In pari · 2 s» | «Replica su lab1: in pari · 2 s» |
| in ritardo | collegata, ritardo > 10 s | ambra | «In ritardo di 45 s» | «Replica su lab1: in ritardo di 45 s» |
| non collegata | P non vede la replica (su R: non riceve) | rosso | «Non riceve da lab2 dal 22/11/2026 10:40: <motivo>» | «Replica su lab1: non collegata dal 22/11/2026 10:40 · tiene 3,2 GB» |
| promossa | dopo «Promuovi…» | verde su R, grigio su P | «Promossa il 22/11/2026 11:02: è il database delle 5 istanze di lab2» | «Sostituito dalla replica su lab1 il 22/11/2026 11:02: fermo, i dati sono di quel momento» |

- **Il ritardo si misura su P** (dal tempo di replay del primario), in secondi interi. Se niente cambia, il ritardo è 0 s, non «sconosciuto».
- **Il disco trattenuto** è quello che il cluster di P conserva per la replica. Sulla riga chiusa compare come badge «tiene 3,2 GB» quando la replica non è collegata oppure quando tiene ≥ 1 GB. Sotto 1 GB e collegata non si vede (HIDE).
- Gli stati in inglese: «Copying: 64 % · 91 MB of 142 MB», «In sync · 2 s», «Behind by 45 s», «Not receiving from lab2 since …: <reason>», «Promoted on …: it is the database of the 5 instances of lab2», «Read replica on lab1: not connected since … · keeps 3.2 GB», «Replaced by the replica on lab1 on …: stopped, its data is of that moment».

## 4. Matrice di visibilità

| Elemento | admin macchina P | admin macchina R | admin workspace / developer / user | Meccanismo |
|---|---|---|---|---|
| Riga «Replica di lettura di lab2» nella tabella di R | — | sì | — | scheda Database solo per `machine_admin` (UX-008 §6.2.1) |
| + Replica di un'altra macchina… | — | sì, se R ha almeno un collegamento in HTTPS o SSH verso un OmniaCLI aggiornato con il cluster. Con collegamenti ma nessuno adatto: `disabled`, con il motivo per collegamento nello `span[title]`. Senza collegamenti: non c'è | — | — |
| Riga del cluster su P: riga `.pg-sub` «Replica su …», badge, `dt` «Replica di lettura» nel dettaglio | sì, solo se la replica esiste | — | — | `machine_admin` |
| Elimina la replica… | sì (P) | sì (R) | — | — |
| Promuovi… | — | sì, solo nel dettaglio | — | — |
| Istanza: «legge anche dalla replica su lab1 (172.30.0.11)» e il link «Database della macchina ›» | sì | — | — | `machine_admin` |
| Istanza: «Letture: anche da una replica», senza macchina, indirizzo né dimensione | — | — | sì, tutti e tre i ruoli, per chi vede Dettagli tecnici | `customerview` più il dato nella route (D5) |
| Leggi dalla replica… / Smetti di leggere dalla replica… | sì, solo istanze di P sul cluster della macchina, Odoo ≥ 18 | — | — | `machine_admin` |
| Lo stesso pulsante su Odoo 17 | `disabled`, con il motivo nello `span[title]` (§6) | — | — | dalla versione del template |
| Istanze dedicate, istanze di P sul cluster di un'altra macchina, database di altre macchine sul cluster | nessun pulsante: la replica è del cluster della macchina | — | — | DEFER |

## 5. I tre posti, dall'alto

### 5.1 R › Database

1. **Testata della sezione**: «+ Replica di un'altra macchina…», primario secondo §1, e accanto «Rileggi».
2. **La riga della replica**, nella stessa tabella, subito dopo la riga del cluster di R, se R ce l'ha.
   - Database: in grassetto «Replica di lettura di lab2», badge «PostgreSQL 17».
   - Sotto, `.pg-sub` con lo stato del §3: «Copia di tutto il cluster di lab2 · in TLS sulla porta 15433, solo per lab2».
   - Versione: «17.12». Dimensione: quella della copia intera, «142 MB». Connessioni: «4 su 300», le letture delle istanze di P.
   - Regolazione: grigio «Segue lab2», con `title` «Una replica ha i valori del suo primario: si regola su lab2».
   - Ripristino a un minuto: «—», con `title` «È quello di lab2: la replica non ne ha uno suo».
   - Colonna dell'azione: vuota. «Proponi i valori…» qui non c'è.
3. **Il dettaglio** (la riga si apre) è un `dl` con quattro righe:
   - Di chi: link a `/links/lab2/#database`;
   - Da quando: «22/11/2026 10:12, copia di 142 MB in 38 s»;
   - Spazio: «142 MB su questa macchina, 297 GB liberi»;
   - Cosa contiene: «Tutti i database del cluster di lab2: 5 istanze (acme-mach, …) e 1 database usato da un'altra macchina (acme_linked). Leggibile solo da root e dall'utente del cluster.»

   Poi le azioni `.pg-actions`: «Promuovi…» (pericolo) ed «Elimina la replica…» (pericolo). Il pericolo non è un primario.
4. **Vuoto**: se R non ha database né replica vale la frase di oggi, a cui si aggiunge, solo se il pulsante è attivo, «Una copia del PostgreSQL di un'altra macchina si fa con + Replica di un'altra macchina…».

### 5.2 Il dialogo «Replica di lettura di un'altra macchina» (su R)

1. **Una select «Di quale macchina»**, che elenca solo i collegamenti adatti. Con un collegamento solo è una riga di testo, non una select.
2. **I fatti, letti da P attraverso il collegamento prima di Crea** (UX-008 regola 6, AD-006 «detto prima»):
   - «La copia è tutto il cluster di lab2: 142 MB, con i dati di tutti i suoi clienti (5 istanze e 1 database di un'altra macchina). Qui ci sono 297 GB liberi, e ne restano liberi almeno 2 GB.»
   - «Tempo stimato: circa 1 minuto.»
   - «Andata e ritorno fra le due macchine: 0,4 ms», misurato come in AD-003. Sopra 1 ms la frase è in ambra: «Ogni pagina di Odoo che legge da qui aspetta circa 2,5 andate e ritorno per query.»
3. **«Indirizzo di questa macchina come la vede lab2»**: proposto e modificabile (per il NAT), come in AD-003.
4. **Cosa cambia su lab2**, sempre detto:
   - con la porta di lab2 già aperta: «Su lab2 nascono un ruolo, una riga di accesso per 172.30.0.11 sola e un punto di ritenuta delle modifiche. Non si riavvia niente.»
   - con la porta chiusa: «Il cluster di lab2 si rifà per aprire la porta in TLS, in circa un secondo: le 5 istanze (acme-mach, …) perdono le connessioni.»
5. **Pulsanti**: il primario del dialogo «Crea la replica» e «Annulla».
   - Con la porta da aprire, l'etichetta del primario diventa «Crea la replica e riavvia il cluster di lab2», e c'è la seconda conferma, come in AD-006.
   - Se c'è un rifiuto (§6), il primario è `disabled` e il motivo sta nello `span[title]` prima del clic.

### 5.3 P › Database, la riga del cluster

- **Riga chiusa**: una `.pg-sub` in più, con lo stato del §3 e il badge «tiene 3,2 GB» quando serve (§3).
- **Dettaglio**: un `dt` «Replica di lettura», dopo «Dove» e prima di «Ripristino a un minuto». Ha quattro righe:
  - «Su lab1 (172.30.0.11), in TLS · da 22/11/2026 10:12 · in pari · 2 s»;
  - «Leggono da lei: acme-mach, beta-shop (2 delle 5 istanze del cluster)», con un link per ognuna, oppure «Nessuna istanza legge da lei: si sceglie nella pagina dell'istanza, Impostazioni › Dettagli tecnici.»;
  - solo se non collegata, o se tiene ≥ 1 GB, in `.pg-warn`: «Il cluster tiene 3,2 GB di modifiche per la replica, non collegata dal 22/11/2026 10:40: crescono di circa 0,4 GB all'ora, e sul disco ci sono 41 GB liberi (pieno fra circa 4 giorni). Se lab1 non torna, Elimina la replica… li libera.»;
  - l'azione «Elimina la replica…» (pericolo).
- **Dopo la promozione**, quando P risponde di nuovo, la riga del cluster ha lo stato grigio «Sostituito…». Il dettaglio dice: «I dati di questo cluster sono del 22/11/2026 11:02 e non li usa più nessuna istanza: le istanze di questa macchina leggono e scrivono sul database di lab1.» L'azione è quella che c'è già, «Togli il cluster…», possibile perché non serve più nessuna istanza. Ricostruire P come replica della nuova è DEFER.

### 5.4 Istanza di P › Impostazioni › Dettagli tecnici, riga «Server del database»

Dopo «cluster della macchina · valori di base» si aggiunge:

- **admin della macchina**: «· legge anche dalla replica su lab1 (172.30.0.11)». Se la replica non è collegata, in ambra: «· replica non collegata: legge dal cluster». Poi il pulsante secondario `.small` «Smetti di leggere dalla replica…». Se non legge: «Leggi dalla replica…», presente solo se la replica esiste ed è in pari o in ritardo.
- **cliente** (admin del workspace, developer, user): «· Letture: anche da una replica». Nient'altro: niente stato, niente macchina, niente pulsante.
- **Odoo 17, admin della macchina**: il pulsante c'è, `disabled`, e il motivo sta nello `span[title]` (§6). Al cliente non si mostra niente.
- **Ritardo**: questa pagina non legge PostgreSQL e quindi non mostra il ritardo. Lo stato viene dalla lettura del monitor (§7), con «letto alle hh:mm» nel `title`.

## 6. Testi esatti (EN sorgente → IT)

**Pulsanti e `title`**

| Pulsante | EN | IT |
|---|---|---|
| crea (R) | + Replica of another machine… | + Replica di un'altra macchina… |
| `title` | Makes on this machine a read-only copy of the PostgreSQL of a linked machine, kept up to date at each change, which its instances can read from. The copy holds every database of that cluster. Nothing starts until Make the replica. | Fa su questa macchina una copia in sola lettura del PostgreSQL di una macchina collegata, tenuta al passo a ogni modifica, da cui le sue istanze possono leggere. La copia contiene tutti i database di quel cluster. Non parte niente finché non scegli Crea la replica. |
| conferma del dialogo | Make the replica | Crea la replica |
| `title` | Asks {link} to let this machine in (a role, a line for {address} only, a hold on its changes), copies its whole cluster here and starts it as a replica reached over TLS. It is a job. | Chiede a {link} di far entrare questa macchina (un ruolo, una riga per {address} sola, una ritenuta delle modifiche), copia qui tutto il suo cluster e lo avvia come replica raggiunta in TLS. È un job. |
| elimina (R) | Drop the replica… | Elimina la replica… |
| `title` (R) | Deletes this copy and its container, and asks {link} to drop what it keeps for it. The instances of {link} reading from here go back to their cluster and restart. A new replica copies everything again. It is a job. | Cancella questa copia e il suo container, e chiede a {link} di togliere quello che tiene per lei. Le istanze di {link} che leggono da qui tornano al loro cluster e si riavviano. Una replica nuova ricopia tutto. È un job. |
| `title` (P) | Drops what this cluster keeps for the replica on {machine} and frees its disk ({held}), and asks {machine} to delete its copy. The instances reading from it go back to this cluster and restart. It is a job. | Toglie quello che questo cluster tiene per la replica su {machine} e ne libera il disco ({held}), e chiede a {machine} di cancellare la sua copia. Le istanze che leggono da lei tornano a questo cluster e si riavviano. È un job. |
| promuovi (R) | Promote… | Promuovi… |
| `title` | Makes this copy the database of the instances of {link}: PostgreSQL of {link} is stopped first, then this copy takes the writes, then those instances are pointed here and restarted. After it the data on {link}'s cluster is old. It is a job. | Fa di questa copia il database delle istanze di {link}: prima si ferma il PostgreSQL di {link}, poi questa copia accetta le scritture, poi quelle istanze vengono puntate qui e riavviate. Dopo, i dati sul cluster di {link} sono vecchi. È un job. |
| leggi (istanza) | Read from the replica… | Leggi dalla replica… |
| `title` | Odoo sends to the replica on {machine} the reads of the pages that do not write, and keeps writing to this machine's cluster. Odoo restarts: for a few seconds it does not answer and whoever is connected loses the connection. It is a job. | Odoo manda alla replica su {machine} le letture delle pagine che non scrivono, e continua a scrivere sul cluster di questa macchina. Odoo si riavvia: per qualche secondo non risponde e chi è collegato perde la connessione. È un job. |
| smetti | Stop reading from the replica… | Smetti di leggere dalla replica… |
| `title` | Odoo reads and writes on this machine's cluster only. Odoo restarts for a few seconds. It is a job. | Odoo legge e scrive solo sul cluster di questa macchina. Odoo si riavvia per qualche secondo. È un job. |
| Odoo 17 (`span[title]`) | Odoo 17 cannot read from a replica: it can from 18 on (db_replica_host). | Odoo 17 non sa leggere da una replica: lo sa fare dalla 18 (`db_replica_host`). |

**Dialoghi** (tutti con `omniaAsk` / `omniaTypeName`, mai `confirm`)

- **Leggi dalla replica…** (`omniaAsk`): «Far leggere acme-mach dalla replica su lab1? Odoo si riavvia e per qualche secondo non risponde. Le scritture restano sul cluster di questa macchina. Se la replica non risponde, Odoo legge dal cluster da solo per 20 minuti e poi riprova. **Se il cluster di questa macchina si ferma, Odoo si ferma: la replica non prende il suo posto da sola. Si promuove a mano, dalla scheda Database di lab1.** Con un ritardo di 2 s, una pagina può mostrare per 2 s un valore appena salvato.»
- **Elimina la replica… da R** (`omniaAsk`, danger): «Eliminare la replica di lab2 (142 MB)? 2 istanze di lab2 (acme-mach, beta-shop) tornano a leggere dal loro cluster e si riavviano per qualche secondo. Una replica nuova ricopia tutto il cluster.»
- **Elimina la replica… da P** (`omniaAsk`, danger): lo stesso, più «Libera 3,2 GB su questo disco.». Se R non risponde: «lab1 non risponde: la sua copia (142 MB) resta là finché il suo admin non la elimina.»
- **Promuovi…, con P che risponde** (`omniaTypeName`, nome da scrivere: il collegamento): «Promuovere la replica di lab2? 1. Il PostgreSQL di lab2 si ferma. 2. Questa copia accetta le scritture. 3. Le 5 istanze di lab2 sul cluster (acme-mach, …) vengono puntate qui e riavviate: circa 30 s senza Odoo. Ritardo adesso: 2 s, e le modifiche di quei secondi non arrivate si perdono. Dopo, i dati sul cluster di lab2 sono vecchi e nessuna istanza li usa. Non si torna indietro da qui. Scrivi lab2 per confermare.»
- **Promuovi…, con P che non risponde** (`omniaTypeName`): «lab2 non risponde dal 22/11/2026 10:40: OmniaCLI non può fermare il suo PostgreSQL. Se è ancora acceso e un'istanza ci scrive, le due copie divergono e le scritture di una delle due si perdono. Promuovi solo se lab2 è davvero spento. Le sue istanze vengono puntate qui quando lab2 risponde di nuovo (D3); fino ad allora non lavorano. Ultimo ritardo letto: 2 s alle 10:39. Scrivi lab2 per confermare che lab2 è spento.»

**Rifiuti** (da `refuse()`, in italiano, con il rimedio)

- «lab2 non risponde: <motivo>. La replica si crea attraverso il collegamento: riprova quando risponde.»
- «Il collegamento con lab2 è in HTTP semplice: la replica chiede HTTPS o SSH, perché la sua risposta dice da dove arrivano i dati.»
- «lab2 non ha il cluster della macchina: non c'è niente da copiare.» / «OmniaCLI su lab2 è più vecchio: aggiornalo per fare una replica.»
- «Il cluster di lab2 ha già una replica, su lab1: eliminala prima, da lì o da lab2.»
- «Non c'è spazio: la copia del cluster di lab2 prende circa 142 MB, qui ci sono 1,8 GB liberi e 2 GB devono restare liberi.»
- «La replica non è ancora una copia intera: si promuove solo dopo che è stata in pari.» (Promuovi… durante la copia, pulsante `disabled`)
- «La replica su lab1 non è collegata dal 22/11/2026 10:40: Odoo leggerebbe comunque dal cluster. Accendi la lettura quando è in pari.» (Leggi dalla replica…, `disabled`)

## 7. Il necessario

- **Stato del sistema.**
  - Lo stato del §3 sta sulla riga chiusa, di tutti e due i lati.
  - Il badge «tiene N GB» si vede.
  - Il job (copia, promozione, eliminazione) ha i passi in `.steps`.
  - Nella pagina dell'istanza c'è la riga «legge anche dalla replica».
  - **Fuori dal pannello**: il monitor di P manda l'avviso quando la replica non è collegata da 10 minuti o quando tiene oltre la soglia (D1, D4). Un disco che si riempie di notte non si scopre aprendo una scheda.
- **Annulla.**
  - Annulla o Esc in ogni dialogo, senza nessuna POST.
  - «Smetti di leggere dalla replica…» annulla «Leggi…».
  - «Elimina la replica…» annulla la creazione.
  - La promozione non si annulla, e lo dice prima, nel dialogo, con il nome da scrivere.
- **Prevenzione dell'errore.**
  - Lo spazio, la versione, il collegamento in HTTPS e la replica unica si controllano prima del clic (`disabled` più `span[title]`).
  - La promozione si ferma se P risponde e non si ferma.
  - Il nome si scrive sempre per promuovere.
  - Il riavvio del cluster di P è detto e chiesto due volte.
  - Odoo 17 non può accendere la lettura.
- **Messaggio d'errore**: i rifiuti del §6. Il motivo tecnico intero sta nel job, per l'admin.
- **Aiuto**: il `title` di ogni pulsante, stato, badge e `summary`; la riga «Cosa contiene»; il vuoto che porta al pulsante.
- **Costo dell'attesa**: la scheda di R e quella di P restano sotto i 2 s. Lo stato della replica si legge con le stesse query della riga (P da `pg_stat_replication` e dallo slot, R dal suo ricevitore), con il suo tempo massimo. Una macchina che non risponde diventa il motivo nella riga (UX-008 regola 2).

## 8. HIDE / DEFER

| Cosa | Rete di sicurezza |
|---|---|
| Parametri e nomi della replica (`wal_level`, `max_wal_senders`, nome dello slot, `primary_conninfo`, `hot_standby*`, LSN, byte di ritardo): mai a schermo, come in UX-008 §6.2.7 | il ritardo in secondi e il disco trattenuto in GB |
| Ruolo, password, CA, certificato e percorsi della replica | nel job dell'admin, filtrato come oggi |
| Disco trattenuto sotto 1 GB con la replica collegata | compare da solo appena la replica si scollega |
| Su P, una riga «Replica: nessuna» quando non c'è | la creazione sta su R; la mappa la mostra quando c'è |
| «Proponi i valori…» sulla replica | «Segue lab2» con il `title` |
| Interruttore per istanza nella scheda Database | sta solo nella pagina dell'istanza; la scheda elenca chi legge, con il link |
| Al cliente: macchina, indirizzo, stato, ritardo, dimensione | «Letture: anche da una replica» |
| DEFER: replica di un cluster dedicato, letture per le istanze di altre macchine (concessioni), più repliche per cluster, P ricostruito come replica della nuova, regolazione della replica | — |

## 9. La mappa (UX-009): cosa cambia

Niente schema nuovo, niente legenda nuova.
- **S1**: la replica fatta da OmniaCLI è un nodo «replica di lab2 · in pari 2 s» **dentro il riquadro di R**, accanto al nodo del suo cluster. La linea puntinata parte dal nodo del cluster di P, nella corsia dei collegamenti. Il riquadro tratteggiato sotto la macchina che la usa resta solo per la replica di terzi.
- **S2 di P**: sotto il nodo «PostgreSQL 17 della macchina» compare il nodo della replica, con la stessa parola di stato del §3. Ha le linee puntinate solo dagli ambienti che leggono da lei.
- **Il verdetto**: «Da guardare» guadagna `kind: "replica"`, quando la replica non è collegata o tiene ≥ 1 GB. Lo vede solo l'admin della macchina.
- **S3 per il cliente**: il passo «Replica» dice «sola lettura · una replica», senza `host:port`. A differenza della replica di terzi (D3 di UX-009), questo è un indirizzo del fornitore.
- **Promossa**: la linea va da R agli ambienti di P, e il nodo del cluster di P è grigio, con «sostituito il …».

## 10. Criteri di accettazione (Playwright; P = lab2 172.30.0.12, R = lab1 172.30.0.11)

Profili: admin della macchina (P e R), admin del workspace acme, developer, user, senza il gruppo developer per i tre del cliente. Viewport 1440×900, lingua IT.

Dati:
- il cluster di lab2 con 5 istanze, una Odoo 17 (se non c'è, un template 17 sul cluster);
- la concessione acme_linked.

1. **Primario**: su R, senza cluster proprio, `.primary:visible` fuori da `dialog[open]` = 1, ed è «+ Cluster della macchina». Con il cluster c'è, il primario è «+ Replica di un'altra macchina…». Su P, nella scheda Database, i primari sono 0. Con un dialogo aperto, `dialog[open] .primary` = 1.
2. **Visibilità**: come admin del workspace, developer e user, la scheda Database c'è 0 volte. Il testo del DOM della pagina dell'istanza e le risposte delle sue route contengono 0 occorrenze di «lab1», «172.30.0.11», «15433» e della dimensione della copia. Contengono 1 volta «Letture: anche da una replica» quando l'istanza legge.
3. **Detto prima**: il dialogo di creazione contiene la dimensione del cluster di lab2 (`/\d+ MB/`), lo spazio libero, «tutti i suoi clienti», il nome «acme_linked» e il tempo di andata e ritorno. Annulla fa 0 POST.
4. **Rifiuti prima del clic**: con lo «spazio da lasciare libero» portato sopra lo spazio libero, «Crea la replica» è `disabled`, ha uno `span[title]` con «Non c'è spazio», e le POST sono 0. Con un collegamento in HTTP semplice vale lo stesso, con «HTTPS o SSH».
5. **Stati**: dopo il job la riga di R dice «In pari» e quella di P «Replica su lab1: in pari». Con il container della replica fermo, entro 70 s la riga chiusa di P dice «non collegata», con il badge, senza aprire la riga.
6. **Disco trattenuto**: con la replica ferma e 200 MB scritti su acme-mach, il dettaglio di P mostra «tiene» con un numero entro ±10 % di quello letto dallo slot. «Elimina la replica…» da P porta quel numero a 0 e la riga perde la `.pg-sub` della replica.
7. **Leggi dalla replica**: dopo il job, `odoo.conf` di acme-mach ha `db_replica_host` non vuoto. `pg_stat_activity` della replica mostra una connessione di quel ruolo e `/web/login` risponde 200. Il dialogo contiene «non prende il suo posto da sola» e «20 minuti».
8. **Odoo 17**: il pulsante è `disabled`, il suo `span[title]` contiene «Odoo 17» e «18». Come cliente, sull'istanza 17 il pulsante e il motivo ci sono 0 volte.
9. **Promuovi, con P che risponde**: il dialogo ha l'input del nome, e il pulsante resta `disabled` finché non c'è scritto «lab2». Dopo il job:
   - il cluster di lab2 è fermo;
   - la replica accetta una scrittura;
   - le istanze di lab2 rispondono 200 entro 60 s, con `db_host` verso lab1;
   - su lab2 la riga dice «Sostituito»;
   - «Togli il cluster…» è presente.
10. **Promuovi, con P che non risponde** (pannello di lab2 fermo): il dialogo contiene «non risponde», «davvero spento» e l'ultimo ritardo con l'ora. Annulla fa 0 POST.
11. **Nessun dialogo del browser**: le chiamate ad `alert`, `confirm` e `prompt` sono 0 in tutti i flussi.
12. **Titoli**: ogni `button`, `summary`, `.pg-state` e `.badge` nuovo ha un `title` non vuoto e diverso dal testo, anche il pulsante di conferma dei dialoghi (rilievo 7 della Parte 1).
13. **HIDE**: nessun testo delle due schede, dei dialoghi e dell'istanza contiene `wal_level`, `max_wal_senders`, `slot`, `primary_conninfo`, `LSN`, `standby` o `streaming`. Fa eccezione `db_replica_host` dentro `.small.mono`.
14. **Date e unità**: ogni data rispetta `\d{2}/\d{2}/\d{4} \d{2}:\d{2}`. Le dimensioni sono in MB o GB, mai MiB o GiB, uguali fra P, R e l'istanza.
15. **Log degli accessi**: creazione, lettura accesa e spenta, eliminazione e promozione compaiono nel log di tutte e due le macchine, con l'utente e il collegamento.

**Smell che fanno FAIL**:
- lo stato della replica visibile solo aprendo la riga;
- il nome di una macchina o un indirizzo a un cliente;
- «nessun failover» detto solo nel README;
- la promozione senza il nome da scrivere;
- un secondo `.primary` nella scheda;
- schermate prese solo come admin.

## 11. Decisioni per Carlo (la raccomandata per prima)

1. **D1 — Un tetto alle modifiche che P tiene per una replica sparita.**
   - **Raccomandata: sì, 20 GB per default (regolabile, come il budget di AD-006).** Oltre quel tetto la replica perde il suo aggancio e va rifatta: sul cluster di lab2 (142 MB) è 1 minuto di copia; su un cluster da 60 GB, circa 15 minuti su una rete da 1 Gbit. P non si riempie mai.
   - Alternativa: nessun tetto, solo l'avviso e «Elimina la replica…». Con 0,4 GB all'ora e 41 GB liberi, il disco è pieno in circa 4 giorni; un fine settimana con un avviso non letto ferma tutti i clienti di P.
2. **D2 — La soglia fra «in pari» e «in ritardo».**
   - **Raccomandata: 10 s.** Una lista di Odoo può mostrare per quei secondi un valore appena salvato.
   - Alternativa: 60 s, con meno avvisi ma letture vecchie fino a un minuto senza segnale.
3. **D3 — P che torna dopo una promozione fatta mentre non rispondeva.**
   - **Raccomandata: finché esiste una replica, il cluster di P non riparte da solo.** OmniaCLI lo avvia solo dopo aver chiesto a R se è stato sostituito, e se sì punta le istanze a R. Senza questo, al riavvio di P le 5 istanze scrivono sul cluster vecchio, e i dati divergono senza che nessuno lo veda.
   - Alternativa: niente di automatico, il dialogo lo dice e l'admin ferma P a mano al ritorno. È un rischio di split brain affidato alla memoria.
   - Va scritto in AD-004 da `software-architect` prima della build.
4. **D4 — L'avviso fuori dal pannello** (mail o webhook del monitor) quando la replica non è collegata da 10 minuti o tiene più di 1 GB.
   - **Raccomandata: sì.**
   - Alternativa: solo nella scheda e nella mappa.
5. **D5 — Il cliente legge «Letture: anche da una replica».**
   - **Raccomandata: sì, una riga senza nomi.** Spiega al cliente perché una pagina può essere indietro di qualche secondo.
   - Alternativa: niente, perché è un fatto del fornitore.

## Non verificato

- Se Odoo 18 e 19 mandano alla replica la lettura che segue subito un salvataggio dello stesso utente: il testo del dialogo dice «può», e va misurato.
- Su quale ramo di Odoo gira il template odooplm-20 (AD-004): finché non è letto, il pulsante segue la versione di Odoo dichiarata dal template.
- Il tempo della promozione (i «circa 30 s» del dialogo): è una stima da misurare su lab1 e lab2 prima di scriverla.

## Tracciabilità

Non ci sono un `TS-NNN` né un `T<n>`/`HT<n>`. È la richiesta di Carlo del 22/11/2026 (AD-004, opzione A, passo 9 dell'ADR).
- `docs/adr/postgresql-della-macchina.md`: AD-004, AD-003 (collegamento, TLS, indirizzo, andata e ritorno) e AD-006 (spazio detto prima, seconda conferma del riavvio).
- UX-008 §2 regole 1, 2, 5 e 6, e §6.2; UX-009 §5, §7 e §13.
- Codice: `omniacli/templates/_machine_database.html` (righe 363-463, riga del cluster e dettaglio), `omniacli/templates/server.html` (righe 983-1004, «Server del database»), `omniacli/templates/_map.html` (righe 1033-1064), `omniacli/customerview.py`.

## Scelte fatte per costruire, confermate da Carlo il 22/11/2026

Carlo ha confermato le cinque scelte qui sotto il 22/11/2026 («a-h ok»), compresa la D3 diversa dalla raccomandata:

- **D1**: tetto di 20 GB alle modifiche tenute per la replica, regolabile. Oltre il tetto la replica va rifatta.
- **D2**: 10 s fra «in pari» e «in ritardo».
- **D3, diversa dalla raccomandata**: il cluster di P **riparte come sempre**, anche con una replica. Se partisse solo
  dopo aver sentito R, un riavvio di P con R spenta fermerebbe tutti i clienti di P per una replica che è facoltativa.
  Dopo una promozione fatta mentre P non rispondeva, R riprova ogni 10 s a fermare il cluster di P e a puntare le sue
  istanze; nella finestra fra il ritorno di P e quella chiamata le istanze di P scrivono sul cluster vecchio, e quelle
  scritture si perdono. Il dialogo «Promuovi…» lo dice con questi numeri.
- **D4**: l'avviso del monitor c'è (replica non collegata da 10 minuti, o oltre 1 GB tenuto).
- **D5**: il cliente legge «Letture: anche da una replica», senza nomi.

## Dopo la verifica (22/11/2026, lab1 = R, lab2 = P)

Costruito in `managedreplica.py`, `_machine_database.html`, `server.html`; i numeri sono nell'ADR (AD-004, opzione A).

- **«Non verificato», le tre voci.**
  - La lettura che segue subito un salvataggio **va alla replica** (Odoo 19, stessa sessione): con la replica indietro
    la pagina mostra il valore di prima. Il dialogo «Leggi dalla replica…» lo dice con i secondi di ritardo di adesso.
  - `odooplm-20`: il ramo non è stato letto (l'immagine non è sulle macchine di prova). Il pulsante segue la versione
    dichiarata dal template.
  - Il tempo della promozione: non «circa 30 s» ma una istanza dopo l'altra, circa 10 s ciascuna (8 s la prima
    produzione, 47 s l'ultima di cinque). Il dialogo dice «circa 10 s ciascuna, N s per l'ultima».
- **Differenze dal contratto.**
  - Il dialogo di creazione ha due campi in più, perché le due soglie sono regolabili solo lì: il tetto delle
    modifiche tenute da P (D1) e lo spazio da lasciare libero su R. Il tetto si cambia poi dal dettaglio di P, «Cambia
    il tetto…».
  - Eliminazione da P: P non può chiamare R. Il dialogo dice «lab1 cancella la sua copia al suo prossimo giro, entro
    un minuto» se R ha interrogato P nell'ultimo minuto e mezzo, altrimenti «lab1 non risponde: la sua copia, se c'è
    ancora, resta là».
  - Stati in più sulla riga di R: «Riceve · lab2 non risponde dal …» (la replica riceve ma il pannello di P non dice
    il ritardo) e «Ha perso il suo aggancio» (oltre il tetto).
  - Dimensioni: la colonna è la somma dei database, «Spazio» è quello che la copia occupa sul disco di R con le
    modifiche che tiene, «Da quando» i byte copiati: tre numeri vicini, non uguali.
  - La pagina del job scrive ancora MiB (il suo formattatore non è stato toccato).
  - Mappa: nodo dentro R e parola di stato sotto il cluster di P nello schema a più macchine (S1), voce `replica` in
    «Da guardare»; la linea puntinata fra le due macchine non è disegnata, e lo schema di una macchina sola (S2) non
    mostra la replica.
- **Criteri del §10**: 1-7 e 9-15 provati sulle due macchine; 8 (Odoo 17) solo nei test, perché non esiste un template
  17. Le schermate sono in IT e EN, come admin della macchina e come admin del workspace `acme`.
