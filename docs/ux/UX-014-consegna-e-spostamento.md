# UX-014 — Consegnare un workspace al cliente, e spostare un'istanza fra le nostre macchine

Stato: contratto di design, prima della build. File proposto: `/home/carlo/Projects/OmniaCLI/docs/ux/UX-014-consegna-e-spostamento.md`.
Lo implementa chi scrive `omniacli/templates/workspaces.html`, `_workspaces_list.html`, `workspace.html`, `server.html`,
`job.html` e i dialoghi nuovi (Jinja, JS senza framework, classi esistenti: `.banner`, `.badge`, `.state-dot`, `.small`,
`.mono`, `details`, `dialog`, `.steps`/`.step` di `job.html`, `omniaTypeName` di `base.html`). Le stringhe vanno in
`omniacli/i18n/it/*.json`, con il sorgente inglese in `_()`.
Valgono le regole comuni e i criteri di UX-001…013, in particolare: UX-007 §2 e §4.4 (pagina del workspace e modulo
«Limiti e disco»); UX-008 §2, regole 1-3 e 6 («detto prima»); UX-009 §6-7 (stati, segni, redazione per lo `scoped`);
UX-010 §2 regola 4 (ogni azione è un job nel registro degli accessi di **tutte e due** le macchine); UX-011 §A e §D
(Prepara · Completa · Annulla, con il nome dell'altra macchina scritto); UX-013 regola A2.2 (nella lista Workspace si
legge, non si cambia). Questo contratto li applica e non li ripete.

Origine, richieste di Carlo del 23/11/2026, in chat:
- «una cosa carina sarebbe la possibilità di spostare i workspace»;
- poi, più precisa: «ad esempio ho un workspace qui Cliente1, poi lui si installa il proprio ambiente e recupera le
  informazioni sul workspace da questo. Così poi qui non vediamo più i suoi server e le sue istanze e lui vede invece
  quello che gli è stato passato. Un altra cosa carina è la possibilità di passare le istanze da un server all'altro ad
  esempio se c'è troppo carico su un server».

**Gate**: sì, UI non banale. Due flussi nuovi, tre schermate toccate per parte, un passaggio irreversibile e dati di un
cliente che lasciano le nostre macchine.

**Serve un AD di `software-architect` prima della build** (questo contratto dice cosa vede chi lavora, non come si
trasporta): il canale e il formato della consegna, cosa dell'identità può passare (hash delle password, segreti della
verifica in due passaggi, passkey legate al nome del pannello, emittente OAuth), come un'istanza su un'altra macchina
resta operabile dal portale del cliente (parte B, D-B2), e come la macchina di prima inoltra i nomi finché il DNS non
punta altrove.

---

## 0. Cosa c'è oggi (23/11/2026, 2026.1.5500)

| Mattone | Dove | Cosa dà a questo contratto |
|---|---|---|
| Archivio completo di un'istanza (`instancearchive.py`) e Nuova istanza › Da un archivio completo | Backup › Altro; pagina del workspace | il pacchetto di **una** istanza: database, filestore, codice ai commit, opzioni. Non porta utenti, ruoli, nomi, token, pianificazioni, destinazioni |
| Staging su un'altra macchina (`linkcopy.upload`, `/api/stagings/receive`) | Nuova staging | un backup spinto attraverso il collegamento, l'istanza rifatta là nel workspace con lo stesso nome (creato se manca) |
| Replica di un database dedicato, promozione e ritorno (UX-011 §D) | Server › Database | la via **quasi senza fermo** per un database: l'altra macchina segue, poi si completa con il nome scritto (circa 10 s senza Odoo, misurato il 22/11) |
| Pagina di manutenzione (dcf5cab) | aggiornamento normale | 503 con una pagina che si ricarica da sola, in italiano o in inglese: 4-8 s misurati su lab4 |
| Aggiornamento senza fermo (`zerodowntime.py`) | Moduli › Aggiornamenti del codice | un secondo Odoo **senza cron**, controllato prima che nginx lo riceva |
| Relay (8bd15ad) | Indirizzi…, Nomi sul web | i nomi di una macchina dietro NAT serviti da un'altra; il cliente legge «una macchina del fornitore» |
| Prova di un nome (Indirizzi…) | dialogo Indirizzi… | un token nella cartella delle verifiche, chiesto su `http://<nome>/`: dice dove punta il DNS |
| Pagina del job con i passi (`result.progress.steps`, `job.html` righe 96-130) | ogni job con trasferimento | i passi con lo stato, la barra dei byte, i numeri |
| Monitoraggio di 14 giorni (`metricshistory.py`) | Server › Monitoraggio | il carico **della macchina**, non dell'istanza |
| Registro degli accessi (NIS 2, `accesslog.py`) | Impostazioni › Accessi | chi ha fatto cosa; resta sulla macchina |

Non esiste oggi: un pacchetto del **workspace** (utenti, ruoli, nomi, servizi esterni), un ingresso «ricevi» su un
OmniaCLI senza collegamenti, lo spostamento di un'istanza **che resta la stessa** (oggi si fa un archivio e se ne crea
un'altra, con un altro nome e il fermo intero).

## 1. Una decisione di struttura: due flussi, non tre

- **Parte A — Consegnare un workspace al cliente.** Il caso principale. Il pacchetto è il workspace intero, e chi lo
  riceve è un altro OmniaCLI qualsiasi, anche senza collegamento con noi.
- **Parte B — Spostare un'istanza fra le nostre macchine.** La produzione, con il suo test, cambia macchina; il
  workspace, i suoi utenti e il suo portale restano dove sono.
- **Spostare un workspace intero fra due macchine nostre non è un terzo flusso** (D-C):
  - se il portale del cliente deve restare lo stesso, è la parte B ripetuta sulle sue istanze;
  - se deve cambiare anche la casa del workspace (utenti, token, portale), è la parte A con una macchina nostra come
    ricevente: «Recupera un workspace» su lab2, con il codice preparato su lab1.

  Un meccanismo per due usi: niente schermate in più da tenere allineate.

---

# Parte A — Consegnare un workspace al cliente

## A1. Persone, device, lavoro primario

| Persona | Chi è nel codice | Dove lavora | Lavoro |
|---|---|---|---|
| **PA1 Integratore** (Carlo, dal pannello centrale attraverso il collegamento, o direttamente su lab1) | admin della macchina che ha il workspace | desktop 1440×900 | decidere cosa si consegna, prepararlo, e sapere quando i dati del cliente non sono più da noi |
| **PA2 Admin del cliente** sul **suo** OmniaCLI appena installato | admin della sua macchina; nessun collegamento con noi | desktop o portatile; il telefono solo per leggere | ricevere il workspace e passare la sua gente sulla sua macchina con il fermo più corto |
| **PA3 Utenti del workspace** (il portale clienti, oggi da noi) | `scoped` | qualunque | sapere quando il servizio si ferma, e da dove si entra dopo |

**Lavoro primario**: per PA1, «so cosa esce, quando, e cosa resta da noi»; per PA2, «ho tutto, sulla mia macchina, e i
miei utenti entrano». Il trasferimento è un mezzo: lo schermo lo mostra come stato, non lo fa governare.

## A2. Regole che la parte A aggiunge

1. **La consegna ha tre momenti, ognuno con chi lo decide.**
   - **Prepara** (PA1): sceglie cosa si consegna, conferma scrivendo il nome del workspace, riceve il codice. Da noi
     non cambia ancora niente.
   - **Ricevi** (PA2): la sua macchina copia, mentre da noi tutto gira.
   - **Passa** (PA2, con il nome scritto, D-A4): da noi le istanze si fermano, la sua macchina prende le ultime
     modifiche e parte.

   Il nome scritto da PA1 in Prepara è il consenso irreversibile del fornitore: il riepilogo che lo precede dice tutto
   quello che succederà dopo, compreso il giorno in cui da noi si cancella.
2. **Nessuna porta in entrata dal cliente per ricevere.** La sua macchina chiama la nostra in HTTPS, con il codice
   (D-A2). Per servire i suoi utenti dopo le servono 80 e 443 aperte: lo dice il dialogo prima di Ricevi.
3. **Dalla parte del cliente niente del fornitore**: nessun nome di macchina, collegamento, cartella o indirizzo
   interno, nel dialogo, nei job, nel JSON. Il fornitore è `provider_label` (il nome di Opzioni › Portale clienti),
   altrimenti «il fornitore».
4. **Dopo Passa, da noi il workspace è una copia ferma con la data in cui sparisce** (D-A3). Nessuna istanza parte da
   sola, nessun backup automatico gira, nessun cron.
5. **Ogni passo è un job, su tutte e due le macchine**, e va nel registro degli accessi di tutte e due: chi ha
   preparato, da dove la macchina del cliente ha preso cosa, chi ha passato, quando la copia è stata cancellata.

## A3. Da noi: dove vive l'azione

- **Posto unico**: il modulo **Limiti e disco** del workspace (`/workspaces?edit=<ws>`, solo admin della macchina), in
  una sezione nuova in fondo, **«Dove vive»**, sopra «Elimina il workspace…»:
  - «Il workspace vive su lab1, disco default (4,1 di 20 GB).»
  - il pulsante secondario **«Consegna al cliente…»**;
  - il pulsante secondario **«Sposta le istanze su un'altra macchina ›»**, che porta alla parte B (§B3).
- **Non** nella testa della pagina del workspace (è un'azione rara, una volta nella vita del cliente) e **non** nella
  riga della lista Workspace (UX-013 A2.2). D-A0.
- **Lo stato** di una consegna in corso è invece dove si guarda: una `.banner.info` in testa alla pagina del workspace e
  la colonna Stato della lista Workspace (§A7). Il pulsante che serve in quello stato («Annulla la consegna…», «Elimina
  adesso la copia…») sta nella banner: è l'unico posto dove l'azione compare prima di essere cercata.

## A4. Da noi: il dialogo «Consegna il workspace Cliente1 al cliente»

Dialogo largo `min(720px, 100vw - 32px)`, tre passi in testa (1 Cosa · 2 Riepilogo · 3 Codice), Indietro e Avanti.

**Passo 1 — Cosa si consegna.** Tre gruppi, ogni riga con la dimensione e la sua scelta predefinita. I numeri sono
quelli dell'esempio di Cliente1, letti prima di aprire il dialogo:

| Gruppo | Riga | Predefinito | Note a schermo |
|---|---|---|---|
| **Istanze** | `cliente1-erp`: produzione e test, database 1,8 GB, file 1,2 GB, codice 0,3 GB | ✓ | «Odoo 19 · database dedicato: passa con un fermo di circa 1 minuto» |
| | `cliente1-shop`: produzione e test, 0,5 GB | ✓ | «database sul cluster della macchina: passa con un fermo di circa 3 minuti» (§A6) |
| | staging `cliente1-erp-stg-20261121` (qui), 2,0 GB | — | «Una staging si rifà dalla produzione in un minuto, sulla macchina del cliente» |
| | staging su un'altra macchina (lab3), 1,9 GB | — non selezionabile | «Resta da noi e si cancella con la copia (§A9)» |
| **Persone** | 6 utenti, 1 ruolo personalizzato | ✓ | «Entrano con la stessa password e la stessa app di verifica (D-A5). 1 passkey va registrata di nuovo: è legata all'indirizzo di questo pannello» |
| | 2 token MCP/API, 1 accesso OAuth (Claude) | — non selezionabile | «Non passano: i client puntano a questo indirizzo. Il cliente li rifà sul suo pannello» (D-A6) |
| **Servizi** | nomi e sottodomini, certificato del cliente | ✓ | §A8 |
| | 2 pianificazioni, 1 destinazione dei backup (S3), credenziali git, webhook della CI su 3 repository | ✓ | «I webhook li rifà la sua macchina con il token GitHub che passa con le credenziali». Gli indirizzi degli avvisi non passano: li mettono gli admin della macchina e possono essere delle nostre persone (revisione del passo 8, 24/11) |
| | storico dei deploy | ✓ | — |
| | backup di questa macchina: l'ultimo di ogni ambiente, 2,4 GB | ✓ | D-A7. «I 36 più vecchi (57 GB) restano qui e si cancellano con la copia» |
| | registro degli accessi del workspace: 4.210 righe dal 12/09/2026 | ✓ | «Un file per la sua conformità NIS 2. Il nostro registro resta da noi» (D-A8) |
| | Odoo Enterprise di `cliente1-erp` | — | «Passa solo se la sottoscrizione è del cliente»: la casella è spenta finché non si spunta «La sottoscrizione è del cliente» |
| | 2 moduli a pagamento (OPL-1) caricati come .zip | ✓, con la spunta | «La licenza è di chi l'ha comprata: il cliente la conferma quando riceve» |

- In fondo, sempre visibile: «Si consegnano 6,1 GB. Con la linea di questa macchina (misurata adesso: 22 MB/s in
  uscita) la copia prende circa 5 minuti, mentre le istanze girano.»
- Una riga che non si può consegnare dice perché, in `.small`, e non ha casella.

**Passo 2 — Riepilogo, «cosa succederà».** Un elenco numerato, le date calcolate dal giorno di oggi:
1. «Ti diamo un codice. Vale fino al 26/11/2026 10:00 (72 ore) e una volta sola.»
2. «Con il codice, il cliente copia sulla sua macchina quello che hai scelto. Qui non si ferma niente.» Se un database
   dedicato scelto non è ancora in ascolto su una porta: «Adesso, a Prepara, il database di `cliente1-erp` si riavvia
   una volta per aprire la sua porta alla copia: Odoo lì perde le connessioni per qualche secondo. Poi, mentre il
   cliente copia, qui non si ferma niente.» (correzione del passo 7, 24/11/2026: il riavvio avviene al clic del
   fornitore, mai alla Ricevi del cliente; il title del primario lo dice).
3. «Quando il cliente sceglie **Passa a questa macchina**, le 2 produzioni qui si fermano, con la pagina di
   manutenzione, per circa 3 minuti (la più lenta, `cliente1-shop`).» Con D-A4 alternativa, la frase dice «quando tu
   scegli…».
4. «Da quel momento Cliente1 qui è una copia ferma: nessuna istanza, nessun backup, nessun utente del cliente entra
   più da questo pannello.»
5. «Il 02/12/2026 (7 giorni dopo) la copia, i suoi backup qui (57 GB) e la staging su lab3 si cancellano. Prima, con
   Elimina adesso la copia…, quando vuoi.»
6. «I nomi sotto il dominio del fornitore (3) non passano: per 7 giorni mandano alla macchina del cliente (§A8).»

Il primario del passo è **«Prepara la consegna…»**, che apre `omniaTypeName` con il nome `Cliente1` da scrivere e il
testo «Dopo il passaggio, Cliente1 non sarà più gestito da questa macchina, e il 02/12/2026 i suoi dati qui si
cancellano.».

**Passo 3 — Il codice**, mostrato una volta sola (lo schema del dialogo del segreto di UX-007 §4.4 D):
- il codice in `.mono`, una riga sola, che contiene l'indirizzo pubblico di questo pannello e il segreto;
- «Copia» (primario del passo) e «Chiudi»;
- la frase «Mandalo al cliente come una password. Chi lo ha può copiare i dati di Cliente1 fino al 26/11 10:00. Si
  perde? Rifai il codice: quello di prima smette di valere.»;
- se questo pannello non ha un indirizzo che si apre da fuori (`portal_url` vuoto e indirizzo privato), il passo 3 non
  c'è e Prepara è disattivato **prima**, al passo 1, con il motivo (§A11).

## A5. Dal cliente: «Recupera un workspace»

**Dove.** Sul suo OmniaCLI appena installato la lista Workspace è vuota. Lo stato vuoto di UX-013 §A9 aggiunge una
riga e un pulsante secondario:
- «Il fornitore ti ha dato un codice di consegna? **Recupera un workspace…**»
- il primario della testa resta **Nuovo workspace** (UX-013 §A6): un solo primario.
- Con workspace già presenti, «Recupera un workspace…» resta un pulsante secondario in testa, accanto al primario.

**Il dialogo, tre passi.**
1. **Il codice**: un campo, incolla. Al primo carattere valido parte la lettura (nessun POST che cambi qualcosa), e
   compare: «Dal fornitore: *Studio Rossi* · workspace **Cliente1** · scade il 26/11/2026 10:00». Codice scaduto,
   usato o sbagliato: il motivo in italiano sotto il campo (§A11), Avanti spento.
2. **Cosa ricevi, e se ci sta.** L'elenco del passo 1 del fornitore, **in sola lettura** (lo ha deciso lui), con quello
   che manca qui:
   - disco: «6,1 GB da ricevere · su questa macchina 180 GB liberi ✓»; con meno, la riga in `--err` e Ricevi spento;
   - «Disco del workspace: 20 GB promessi, come da noi», modificabile in su, mai sotto l'usato (4,1 GB);
   - per ogni cosa che manca, una riga con il rimedio: «Docker e nginx: manca il pacchetto dei workspace —
     `sudo apt install ./OmniaCLI-workspaces.deb`», «Template odoo-19: si installa firmato durante la ricezione ✓»,
     «OmniaCLI 2026.1.5477: serve 2026.1.55xx o più recente — aggiornalo»;
   - «Raggiungibile da fuori sulle porte 80 e 443: no» in `--warn`, con «Serve per i nomi e i certificati. La copia si
     fa lo stesso; prima di passare lo ricontrolliamo.»;
   - le due spunte che chiedono una conferma del cliente: «Ho la licenza dei 2 moduli a pagamento», e se c'è,
     «La sottoscrizione Odoo Enterprise è mia».
3. **Ricevi** (primario del dialogo): parte il job della ricezione e si apre la sua pagina.

**Lo stato sulla macchina del cliente, nella lista Workspace e nella pagina del workspace ricevuto**:

| Stato | Parola (riga e banner) | Azione nella banner |
|---|---|---|
| ricezione in corso | «In arrivo dal fornitore: 64 % · 3,9 di 6,1 GB» | Vedi il job › |
| pronto | «Pronto per passare · al passo da 12 s» | **Passa a questa macchina…** (primario della banner, l'unico) · Annulla la ricezione… |
| fuori passo | «Pronto, ma non riceve dal fornitore dal 25/11 21:40: <motivo>» | Riprova · Annulla la ricezione… |
| passaggio | «Passaggio in corso: le istanze del fornitore sono ferme da 40 s» | Vedi il job › |
| passato | «Su questa macchina dal 25/11/2026 22:04 · 2 nomi da sistemare nel DNS» | Controlla i nomi › |

**Passa a questa macchina…** apre un riepilogo corto e chiede il nome `Cliente1` scritto:
«Le 2 istanze di Cliente1 dal fornitore si fermano adesso, con una pagina di manutenzione. Questa macchina prende le
modifiche fatte da quando è pronta, poi le avvia. Fermo previsto: circa 3 minuti. I tuoi 6 utenti entreranno da
https://erp.cliente1.it/omnia, con la loro password; 1 deve registrare di nuovo la passkey.»
Il pulsante è spento, con il motivo nel `title` dello `span`, se: il fornitore non risponde; le porte 80 e 443 non sono
raggiungibili da fuori (§A8); un database su una macchina del cliente non lascia entrare questa (§A9); il codice è
scaduto (la copia resta, ma serve un codice nuovo per le ultime modifiche).

## A6. Il passaggio, e quanto si resta senza servizio

Per ogni istanza la via la decide lei, ed è scritta nel riepilogo e nel job:

| Istanza | Via | Fermo per gli utenti |
|---|---|---|
| database dedicato (il servizio `db` del suo progetto) | durante Ricevi la macchina del cliente segue il database come la replica di UX-011 §D e copia i file; a Passa: manutenzione da noi, ultimo record applicato, file mancanti, Odoo avviato **senza cron** e controllato, poi i nomi | circa 1 minuto (database 10 s misurati il 22/11, più l'avvio di Odoo e i nomi: da misurare) |
| database sul cluster della macchina, o PostgreSQL di versione diversa | i file durante Ricevi; a Passa: manutenzione, backup, trasferimento, ripristino | il tempo del backup: per 0,3 GB circa 3 minuti, per 1,8 GB circa 8 minuti a 22 MB/s (stimato, da misurare) |
| database su una macchina del cliente (template *database di un'altra macchina*) | non si copia: la macchina del cliente si collega lo stesso database | il riavvio di Odoo, circa 15 s |

- **Tutto o niente.** Le istanze partono sulla macchina del cliente senza cron e senza traffico; i nomi passano solo
  quando **tutte** rispondono a `/web/login` con 200. Se una non parte, tutte tornano a girare da noi, la manutenzione
  si toglie, e niente è perso: nessuno ha scritto sulla macchina del cliente.
- La pagina di manutenzione è quella di dcf5cab, con un testo suo: «Il servizio si sta spostando. Torna fra qualche
  minuto: la pagina si ricarica da sola.»

## A7. Il job, sulle due macchine

**Dal cliente** (la pagina del job, `.steps`):
- Ricevi: **1 Controlli** · **2 Template e immagini** · **3 Copia** (barra dei byte, «3,9 di 6,1 GB · 22 MB/s · circa
  2 minuti») · **4 Al passo** (finisce con «pronto»).
- Passa: **1 Manutenzione dal fornitore** · **2 Ultime modifiche** · **3 Avvio qui** (una riga per istanza, «2 di 2
  rispondono») · **4 Nomi e certificati**.
- Le righe del log dicono «il fornitore», mai una macchina, una cartella o un indirizzo nostro.

**Da noi** (job della macchina, letti come oggi da PA1): «Consegna di Cliente1: preparata», «la macchina del cliente
(203.0.113.40) ha preso 6,1 GB», «passato alla macchina del cliente: istanze ferme», «copia cancellata». La banner della
pagina del workspace:

| Stato da noi | Banner | Azione |
|---|---|---|
| preparata | «Consegna preparata: il codice scade il 26/11/2026 10:00. Il cliente non l'ha ancora usato.» | Rifai il codice… · Annulla la consegna… |
| in ricezione | «Il cliente sta ricevendo: 64 %. Qui tutto gira.» | Annulla la consegna… |
| pronta | «Il cliente è pronto per passare: le istanze qui si fermeranno quando lo sceglie lui.» | Annulla la consegna… |
| consegnata | «Consegnato al cliente il 25/11/2026 22:04. Qui resta una copia ferma fino al 02/12/2026.» | Elimina adesso la copia… · Riaccendi la copia… (§A10) |

Nella lista Workspace (UX-013) la colonna Stato dice la stessa parola, senza pulsanti. Una copia ferma **non** conta in
«N da guardare»; ci entra una consegna preparata da più di 72 ore e mai usata, e una copia che domani si cancella.

## A8. I nomi, i certificati, il DNS

- **Nomi sotto il dominio del cliente** (`erp.cliente1.it`): passano. La macchina del cliente scrive il suo nginx; il
  record DNS lo cambia il cliente. Fino a quando non punta, la nostra macchina **inoltra** le richieste di quei nomi alla
  sua, comprese le verifiche di Let's Encrypt: così il certificato arriva là prima del cambio, e chi ha ancora il DNS
  vecchio non vede un errore (D-A9). L'inoltro finisce quando tutti i nomi puntano là, o con la cancellazione della
  copia.
- **Nomi sotto il dominio del fornitore** (`cliente1.studiorossi.it`, sslip.io del nostro IP): non passano. La macchina
  del cliente prende i suoi nomi automatici (sslip.io del suo IP) e, se il workspace ha un dominio del cliente, i nomi
  sotto quello. Per 7 giorni i nostri rispondono con un rinvio permanente al nome nuovo.
- **Certificato del cliente** (caricato su Nomi sul web): passa. **Let's Encrypt**: se ne fa uno nuovo là.
- **Il controllo dei nomi**, sulla macchina del cliente, scheda Nomi sul web del workspace ricevuto, una tabella:
  Nome · Punta adesso a · Deve puntare a · Stato («✓ punta qui», «✗ punta ancora al fornitore»), più il TTL letto, e
  «Ricontrolla». Si ricontrolla da sola ogni 10 minuti per 7 giorni. Il cliente legge «punta ancora al fornitore», mai
  il nostro IP.
- **Prima di passare**, se un TTL è sopra 300 s, il riepilogo lo dice: «Il record di erp.cliente1.it ha un TTL di
  3600 s: abbassalo a 300 un giorno prima, così il cambio arriva in 5 minuti invece che in un'ora.»
- **Relay**: un workspace pubblicato attraverso un nostro relay non lo porta con sé. Se la macchina del cliente non è
  raggiungibile da fuori, Passa è spento con il motivo «Serve un indirizzo pubblico, o un relay suo: il relay del
  fornitore non passa con la consegna.»

## A9. Cosa passa e cosa no (il testo di «Come funziona ▸» del dialogo, chiuso)

| Passa | Non passa, e perché |
|---|---|
| Istanze scelte: produzione e test, database, filestore, repository con la storia, moduli caricati, opzioni di Odoo | Staging su altre macchine nostre: si cancellano con la copia |
| Utenti del workspace con ruolo, ruoli personalizzati, hash delle password e segreti della verifica in due passaggi (D-A5; chi è membro anche di un altro nostro workspace passa senza l'app, e il riepilogo di Prepara lo nomina) | Passkey (legate all'indirizzo del pannello), sessioni, inviti non ancora accettati (il link punta a noi) |
| Nomi, sottodomini, indirizzi in più e rinvii, certificato del cliente | Nomi sotto il nostro dominio; certificati Let's Encrypt (se ne fanno di nuovi) |
| Pianificazioni (all'ora del cliente: §A11), destinazioni con le loro chiavi, credenziali git, storico dei deploy (chi li ha fatti letto come lo legge il cliente) | Indirizzi degli avvisi (li mette il cliente); token MCP e API, accessi OAuth (D-A6); replica di un database su una nostra macchina (si cancella) |
| L'ultimo backup di ogni ambiente (D-A7) e l'archivio del registro degli accessi del workspace (D-A8) | Gli altri backup locali, lo storico dei job, il nostro registro degli accessi, il carico misurato |
| Odoo Enterprise e moduli a pagamento, con la conferma del cliente | Il marchio del fornitore (`brand_name`, logo): il pannello del cliente è OmniaCLI, AGPL-3.0, come lo installa |

Utenti della macchina del fornitore che erano membri del workspace (Carlo, i colleghi): **non passano**. Sul pannello del
cliente c'è il suo admin, fatto all'installazione.

## A10. Tornare indietro

- **Prima di Passa**: «Annulla la consegna…» (da noi) o «Annulla la ricezione…» (dal cliente). Il codice smette di
  valere, la macchina del cliente cancella quello che ha copiato, da noi non cambia niente perché niente si era fermato.
  Conferma semplice: non si perde nessun dato.
- **Dopo Passa, la via normale è la consegna al contrario**: il cliente prepara un codice sul suo pannello, noi
  Recuperiamo il workspace su una nostra macchina. Con la copia ferma ancora qui, si copia solo quello che è cambiato.
  Nessun pulsante in più.
- **Se la macchina del cliente muore nei 7 giorni**: «Riaccendi la copia…» nella banner, da noi, con il nome scritto e
  la frase «Riparte Cliente1 com'era il 25/11/2026 22:04: quello che il cliente ha scritto dopo sulla sua macchina qui
  non c'è. I nomi tornano qui solo se il DNS punta qui.» È l'unica azione che riaccende una copia ferma.

## A11. Vuoto, rifiuti, guasti a metà

| Caso | Dove si legge | Testo (IT, con il rimedio) |
|---|---|---|
| Questo pannello non si apre da fuori | passo 1, Prepara spento | «La macchina del cliente deve raggiungere questo pannello in HTTPS: Impostazioni › Opzioni › Portale clienti, o un nome con certificato.» |
| Un job gira su un'istanza del workspace | passo 1 | «cliente1-erp ha un job in corso (Aggiorna): aspetta che finisca.» |
| Codice scaduto / già usato / di un'altra macchina | dialogo del cliente | «Il codice è scaduto il 26/11 10:00: chiedine uno nuovo al fornitore.» · «Il codice è già stato usato da un'altra macchina.» |
| Lo stesso nome qui | dialogo del cliente | «Su questa macchina c'è già un workspace Cliente1: lo ricevi come Cliente1-2, o togli quello.» (scelta a radio) |
| Disco insufficiente | dialogo del cliente | «Servono 6,1 GB, qui ce ne sono 4,0 liberi.» |
| Fuso orario diverso | dialogo del cliente, riga delle pianificazioni | «Le 2 pianificazioni girano alle 03:00 ora di Roma: questa macchina è in UTC, sul server saranno le 02:00.» |
| Database su una macchina del cliente (un caso fuori dall'esempio) | riepilogo di Passa | «cliente1-crm usa il database su db.cliente1.it: deve lasciar entrare 203.0.113.40. Lo proviamo prima di passare.» |
| La copia si interrompe | job del cliente, passo 3 rosso | «Copia interrotta a 3,9 di 6,1 GB: il fornitore non risponde. Riprova riprende da qui.» Da noi: niente è cambiato. Dal cliente: la parte copiata resta finché Riprova o Annulla |
| Un'istanza non parte durante Passa | job del cliente, passo 3 rosso | «cliente1-shop non è partita: <ultime righe del log di Odoo>. Le istanze girano di nuovo dal fornitore, niente è perso. Riprova Passa quando è corretto.» |
| Il fornitore non risponde durante Passa | job del cliente | Passa non parte senza il fornitore: «Il fornitore non risponde: le sue istanze non si possono fermare, e i dati si dividerebbero.» |

## A12. Cosa vedono gli utenti del workspace (PA3), da noi

- **Avviso facoltativo** (D-A10): una spunta nel passo 2 del fornitore, «Avvisa gli utenti di Cliente1», con un campo
  data e ora (solo testo, non fa partire niente). Mostra in testa al loro pannello: «Il 25/11/2026 alle 22:00 il tuo
  spazio passa su una macchina tua. Il servizio si ferma per circa 3 minuti. Dopo, si entra da un indirizzo nuovo che ti
  darà il tuo amministratore.»
- **Dopo Passa**, un utente di Cliente1 che entra dal nostro portale legge, dopo la password: «Il tuo spazio di lavoro è
  passato su un'altra macchina. Chiedi al tuo amministratore il nuovo indirizzo.» Niente indirizzo del cliente: non lo
  conosciamo come pubblico (D-A10 lo può cambiare).

## A13. Visibilità (parte A)

| Elemento | PA1 admin della macchina (diretto o collegamento admin) | collegamento sotto admin | admin del workspace (da noi) | sviluppatore, utente del workspace | token MCP/API | PA2 admin della sua macchina |
|---|---|---|---|---|---|---|
| Sezione «Dove vive», Consegna al cliente… | sì | — | — (D-A1) | — | `prepare_handover` con token admin, con il nome come conferma | — |
| Banner della consegna, da noi | sì | in lettura | solo l'avviso di §A12 | solo l'avviso | — | — |
| Recupera un workspace… | — | — | — | — | `receive_workspace` con token admin della sua macchina | sì |
| Passa a questa macchina… | — | — | — | — | con `confirm` = nome | sì |
| Elimina adesso la copia…, Riaccendi la copia… | sì | — | — | — | — | — |

Meccanismo: `machine_admin` nella route; il ruolo più basso fra chi guarda e il collegamento; `customer_view` per lo
`scoped`. Le route stanno fuori da `WORKSPACE_ROUTES`. REST e MCP ci sono per tutte e tre le azioni (regola di Carlo
del 22/11: ogni capacità REST ha il suo strumento MCP).

## A14. HIDE / DEFER (parte A)

| Cosa | Dove va | Rete di sicurezza |
|---|---|---|
| Consegna al cliente… in testa o nella lista | Limiti e disco › Dove vive | lo stato in corso sta nella banner, dove si guarda |
| Il dettaglio tecnico di cosa passa | «Come funziona ▸», chiuso | ogni riga del passo 1 ha la sua nota |
| Righe che non si possono consegnare | senza casella, con il motivo | — |
| Il pacchetto come file da scaricare e caricare (macchina senza rete) | DEFER | il motivo nel `title` di «Come funziona» |
| Una lista delle consegne fatte | nessuna: il registro degli accessi | la banner resta fino alla cancellazione della copia |

## A15. Telefono (fino a 700 px)

- Il dialogo del fornitore e quello del cliente vanno a una colonna. Le righe del passo 1 sono schede di due righe
  (nome e scelta; dimensione e nota in `.small`). Niente scorrimento orizzontale.
- Passa a questa macchina… si fa anche da telefono: è un nome scritto e un pulsante. Il controllo dei nomi è una scheda
  per nome.

---

# Parte B — Spostare un'istanza fra le nostre macchine

## B1. Persona e lavoro

- **PB1 Integratore**, admin delle due macchine (dal pannello centrale, con collegamenti admin a entrambe), desktop.
- **Lavoro**: «lab1 è troppo carica: sposto `acme-erp` su lab2, il fermo è di un minuto, e se va male torno».
- **Cosa si sposta**: la produzione con il suo test. Le staging della produzione restano dove sono e seguono la
  produzione nuova (D-B5).
- **Il cliente** (PB2, utenti del workspace): deve continuare a entrare dallo stesso portale, con le stesse credenziali,
  e vedere la stessa istanza (D-B2).

## B2. Regole che la parte B aggiunge

1. **L'istanza resta la stessa**: stesso nome, stesso workspace, stessi nomi sul web, stessi deploy e backup. Cambia la
   riga «Macchina».
2. **Prepara · Completa · Annulla**, come il ritorno di UX-011: Prepara copia mentre tutto gira; Completa ferma e passa,
   con il nome **dell'istanza** scritto (è una produzione: a mano, quando decide l'integratore).
3. **La macchina di prima tiene una copia ferma N giorni** (D-B4), con **«Torna su lab1…»**.

## B3. Dove vive

- **Pagina dell'istanza › Altro ▾ › «Sposta su un'altra macchina…»**, solo admin della macchina, dopo le voci del
  backup (D-B1). È dove l'integratore guarda l'istanza che pesa.
- Dal modulo Limiti e disco del workspace (§A3) il link «Sposta le istanze su un'altra macchina ›» apre l'elenco
  Istanze cercato su quel workspace: la scelta resta per istanza.
- **Non** sulla mappa (UX-009 è sola lettura), **non** nella lista Workspace (UX-013 A2.2).
- **Lo stato** sta nella tabella Ambienti della pagina dell'istanza, riga della produzione: «⇢ in spostamento su lab2:
  copia 64 %», «⇢ pronta su lab2 · al passo da 2 s», poi «su lab2 dal 25/11 · copia ferma su lab1 fino al 02/12». La
  banner in testa alla pagina porta l'azione dello stato (Completa…, Annulla…, Torna su lab1…).

## B4. Il dialogo «Sposta acme-erp su un'altra macchina»

1. **Perché spostarla, in una riga** (il confronto prima della scelta): «lab1 negli ultimi 14 giorni: CPU 91 % al 95°
  percentile, memoria 88 %. acme-erp adesso usa 1,4 core e 2,1 GB.» Il consumo per istanza sui 14 giorni oggi non c'è
  (Non verificato): la riga dice «adesso», mai una stima inventata.
2. **Su quale macchina**: una radio per ogni collegamento della macchina dell'istanza, in HTTPS o SSH, con ruolo admin.
   Ogni macchina in due righe:
   - «lab2 — CPU 34 % · memoria 41 % (95° percentile, 14 giorni) · 180 GB liberi»;
   - in `.small`: «Odoo 19 ✓ · PostgreSQL 17 ✓ · andata e ritorno 0,4 ms · linea 60 MB/s».

   Le macchine che non possono sono disattivate con il motivo accanto: «OmniaCLI più vecchio: aggiornalo», «12 GB
   liberi, ne servono 3,3», «manca il template odoo-19 (si installa durante Prepara)», che però resta selezionabile,
   «il collegamento è in HTTP: i dati del cliente non passano in chiaro», «lab1 non ha un collegamento verso lab4:
   aggiungilo in Impostazioni › Collegamenti di lab1». Parte scelta la macchina col 95° percentile di CPU più basso.
3. **Il piano**, dopo la scelta:
   - «Si copiano 3,3 GB (database 1,8 · file 1,2 · codice 0,3): circa 1 minuto a 60 MB/s. Qui tutto gira.»
   - «Al completamento: fermo di circa 1 minuto (database dedicato)», oppure «circa 4 minuti (database sul cluster della
     macchina: backup e ripristino di 1,8 GB)».
   - «Il workspace acme su lab2: esiste, ci va accanto» oppure «nasce su lab2, con i limiti di quello di lab1».
   - «Le 2 staging restano su lab1 e diventano staging della produzione su lab2.»
   - «I nomi (3) puntano a lab1. Dopo il completamento lab1 li inoltra a lab2 finché il DNS non punta là (D-B3).»
4. **Pulsanti**: **Prepara lo spostamento** (primario del dialogo) · Annulla. Prepara non ferma niente: nessuna
   conferma in più.

**Completa lo spostamento…** (banner, stato «pronta»): `omniaTypeName` con il nome `acme-erp` e il testo «acme-erp si
ferma per circa 1 minuto, con la pagina di manutenzione, poi riparte su lab2. Su lab1 resta ferma fino al 02/12/2026.».
Spento, con il motivo, se lab1 o lab2 non risponde, o se l'istanza non è al passo da più di 60 s.

## B5. Il job e il fermo

- Prepara: **1 Controlli** · **2 Su lab2** (workspace, template, immagini) · **3 Copia** (barra) · **4 Al passo**.
- Completa: **1 Manutenzione su lab1** · **2 Ultime modifiche** · **3 Avvio su lab2** (senza cron, controllo
  `/web/login`) · **4 Nomi** · **5 Fermo su lab1**. Se il passo 3 fallisce, l'istanza torna a girare su lab1, la
  manutenzione si toglie, niente è perso. L'ultima riga del job dice il fermo misurato («Senza servizio per 52 s»), come
  `zerodowntime.py`, e va anche nel deploy dell'istanza.

## B6. DNS, ritorno, cliente

- **DNS**: la tabella dei nomi di §A8, nella banner dell'istanza dopo il completamento, con «Ricontrolla». lab1 inoltra
  finché tutti i nomi puntano a lab2 o finché la copia c'è. Opzione nel piano: «Lascia i nomi su lab1» (lab1 resta il
  davanti, come un relay, e nessun record cambia), D-B3.
- **Torna su lab1…** (banner): lo stesso Prepara/Completa al contrario. Con la copia ferma come base, dei **file** si
  copia solo quello che è cambiato su lab2; il **database dedicato** torna intero (Carlo, 23/11/2026, AD-015). Copia
  già eliminata: l'istanza si rifà su lab1 dal suo template e si copia tutta (la pagina dell'istanza resta su lab1
  per i suoi admin, con la banner). Dopo il ritorno lab2 tiene la sua copia ferma e inoltra i nomi a lab1 finché
  qualcuno non preme **Elimina la copia su lab2…** (il DNS può puntare a lab2). Nome dell'istanza scritto.
- **Elimina la copia su lab1…**: conferma semplice, prima della scadenza.
- **Il cliente** (PB2) legge nella sua pagina, durante il completamento, lo stato «in manutenzione» come per un
  aggiornamento, e poi un job «Spostata su un'altra macchina del fornitore». Nessun nome di macchina. Il portale e le
  credenziali non cambiano (D-B2, serve l'AD).

## B7. Visibilità (parte B)

| Elemento | admin di tutte e due le macchine | admin della sola macchina dell'istanza | collegamento sotto admin | admin, sviluppatore, utente del workspace | token |
|---|---|---|---|---|---|
| Altro › Sposta su un'altra macchina… | sì | sì; le macchine dove non è admin sono disattivate con il motivo | — | — | REST/MCP `move_instance`, token admin, Completa con `confirm` = nome |
| Stato nella riga Ambienti | sì, con i nomi delle macchine | sì | in lettura | «in manutenzione», poi «su un'altra macchina del fornitore» | — |
| Completa…, Torna su lab1…, Elimina la copia… | sì | — | — | — | idem |

## B8. Telefono

La voce di Altro ▾ e il dialogo ci sono anche sotto i 700 px, a una colonna; le due righe di ogni macchina vanno a
capo. Completa e Torna si fanno anche da telefono. Niente scorrimento orizzontale.

---

## Il necessario (A e B)

- **Stato del sistema**: i passi del job con la barra; la banner con la parola dello stato e il suo momento; la colonna
  Stato della lista Workspace; la riga della produzione in Ambienti; il fermo misurato come ultima riga del job.
- **Annulla**: Annulla in ogni dialogo e con Esc, senza richieste POST. Prima del passaggio, Annulla la consegna / la
  ricezione / lo spostamento, senza perdita. Dopo, la consegna al contrario (A) o Torna su lab1 (B), con la copia ferma.
- **Prevenzione dell'errore**: le macchine e le righe che non possono sono disattivate con il motivo, prima del clic;
  il nome scritto in Prepara la consegna, Passa, Completa, Torna, Riaccendi; tutto-o-niente al passaggio; il TTL detto
  un giorno prima; Passa spento senza porte aperte o senza il fornitore.
- **Messaggio d'errore**: in italiano, dice cosa resta dove (§A11) e cosa fare; mai un nome tecnico del pacchetto.
- **Aiuto**: un `title` su ogni pulsante, radio e casella, scritto dalla route (§«Pulsanti»); «Come funziona ▸» nel
  dialogo; gli stati vuoti dicono il primo passo.

## Pulsanti (`title`, dalla route)

| Pulsante | Dove | Tipo | `title` |
|---|---|---|---|
| Consegna al cliente… | Limiti e disco › Dove vive | secondario | «Prepara la consegna di questo workspace a un OmniaCLI del cliente: scegli cosa passa, poi ricevi un codice. Il riepilogo dice cosa si ferma qui e quando.» |
| Prepara la consegna… | dialogo, passo 2 | primario del passo | «Chiede il nome del workspace e crea il codice, valido 72 ore e una volta sola. Con il nome confermi che dopo il passaggio il workspace lascia questa macchina e si cancella il giorno scritto sopra.» |
| Copia | dialogo, passo 3 | primario del passo | «Copia il codice negli appunti. Non si vedrà più.» |
| Rifai il codice… | banner | secondario | «Crea un codice nuovo; quello di prima smette di valere. Quello che il cliente ha già copiato resta valido.» |
| Annulla la consegna… | banner | pericolo `.small` | «Il codice smette di valere e la macchina del cliente cancella quello che ha copiato. Qui non cambia niente.» |
| Elimina adesso la copia… | banner | pericolo `.small` | «Cancella subito la copia ferma di Cliente1, i suoi backup qui e la staging su lab3, invece del 02/12/2026. Non si annulla.» |
| Riaccendi la copia… | banner | pericolo `.small`, nome scritto | «Riavvia qui Cliente1 com'era al passaggio, se la macchina del cliente non c'è più. Quello che il cliente ha scritto dopo qui non c'è.» |
| Recupera un workspace… | lista Workspace del cliente | secondario | «Riceve un workspace da un altro OmniaCLI con il codice di consegna che ti ha dato il fornitore. Prima dice cosa arriva e se ci sta.» |
| Ricevi | dialogo del cliente | primario del dialogo | «Copia qui il workspace mentre dal fornitore tutto gira. È un job; da lui non si ferma niente.» |
| Passa a questa macchina… | banner del cliente | primario della banner, nome scritto | «Ferma le istanze dal fornitore, prende le ultime modifiche e le avvia qui. Fermo di circa N minuti. Chiede il nome del workspace.» |
| Annulla la ricezione… | banner del cliente | pericolo `.small` | «Cancella quello che è stato copiato qui. Dal fornitore non cambia niente.» |
| Controlla i nomi › | banner del cliente | link | «Apre i nomi del workspace con dove punta ognuno adesso» |
| Sposta su un'altra macchina… | istanza › Altro ▾ | voce del menu | «Copia questa produzione con il suo test su un'altra macchina mentre gira, poi la passa là quando completi. Le staging restano dove sono. Solo amministratori della macchina.» |
| Prepara lo spostamento | dialogo B | primario del dialogo | «Copia l'istanza sulla macchina scelta e la tiene al passo. Qui non si ferma niente. È un job.» |
| Completa lo spostamento… | banner B | primario della banner, nome scritto | «Ferma l'istanza qui con la pagina di manutenzione, la avvia sulla macchina nuova e le passa i nomi. Fermo di circa N secondi. Chiede il nome dell'istanza.» |
| Annulla lo spostamento… | banner B | pericolo `.small` | «Cancella la copia sulla macchina scelta. L'istanza resta qui com'è.» |
| Torna su lab1… | banner B | secondario, nome scritto | «Riporta l'istanza sulla macchina di prima, con le modifiche fatte da allora. Stesso fermo dello spostamento.» |
| Ricontrolla | tabella dei nomi | secondario `.small` | «Chiede di nuovo dove punta ogni nome. Non cambia niente.» |

## Criteri di accettazione (Playwright)

**Dove**: parte A da **lab3** (fornitore) a **lab4** (cliente: nessun collegamento con lab3, dietro il NAT simulato, solo
uscita); parte B da lab3 a lab4 **collegate**, dal pannello locale avviato dal worktree. L'aspetto finale sul pannello
centrale di Carlo con lab1/lab2, solo dopo il rilascio. Persone: admin della macchina, admin del workspace, utente del
workspace, admin della macchina del cliente. Mai il gruppo developer. 1440×900 e 375×812, IT, tema chiaro e scuro.
Dati: un workspace con 2 produzioni (una con database dedicato, una sul cluster), 1 staging, 6 utenti di cui 1 con
passkey, 2 token, 1 pianificazione, 1 destinazione.

**Parte A**
1. **Posto**: «Consegna al cliente…» c'è 1 volta, solo in Limiti e disco; 0 volte nella testa della pagina del
   workspace e nella lista Workspace; 0 per l'admin del workspace e per un collegamento sotto admin.
2. **Primari**: in ogni passo del dialogo `dialog[open] .primary:visible` è 1; nella lista Workspace del cliente vuota,
   `.primary:visible` fuori dai dialoghi è 1 («Nuovo workspace») e «Recupera un workspace…» non è `.primary`.
3. **Detto prima**: le dimensioni del passo 1 sommano al totale «Si consegnano N GB» (±0,1); il riepilogo contiene una
   data `\d{2}/\d{2}/\d{4}` di scadenza del codice e una di cancellazione = passaggio + 7 giorni.
4. **Nome scritto**: Prepara la consegna, Passa, Riaccendi hanno il pulsante `disabled` finché il campo non è uguale al
   nome; Annulla ed Esc: 0 richieste POST.
5. **Nessuna porta dal cliente**: con lab4 senza porte in entrata, Ricevi arriva a «pronto»; sul firewall di lab4 0
   regole nuove in entrata durante Ricevi.
6. **Redazione dal cliente**: in DOM, risposte JSON e log dei job di lab4, 0 occorrenze di «lab3», dell'IP di lab3, dei
   nomi dei collegamenti di lab3, di «/srv/», «/var/lib/omniacli»; il fornitore compare come `provider_label`.
7. **Passaggio**: una sonda su ogni nome ogni 100 ms registra le risposte; 0 risposte 502, solo 200 e 503 (manutenzione);
   il tempo di 503 è la riga «Senza servizio per N s» del job (±2 s). Un'istanza resa impossibile da avviare: entrambe
   le istanze tornano 200 su lab3, 0 righe scritte su lab4 dopo l'avvio.
8. **Dopo**: su lab3, `systemctl`/docker: 0 container di Cliente1 in esecuzione; la banner dice la data di
   cancellazione; un utente di Cliente1 al login su lab3 legge il testo di §A12 e 0 istanze. Su lab4: 6 utenti, 1 ruolo
   personalizzato, gli utenti entrano con la stessa password e lo stesso codice TOTP; l'utente con passkey: 0 passkey.
9. **Cosa non passa**: su lab4, 0 token MCP/API, 0 accessi OAuth, 0 staging; su lab3, dopo «Elimina adesso la copia…»,
   0 file sotto il disco di Cliente1, 0 backup di Cliente1, e 1 voce nel registro degli accessi.
10. **Registro**: ogni passo (preparata, ricevuta, passata, cancellata) ha 1 voce nel registro degli accessi di lab3 e
    le tre del cliente in quello di lab4.

**Parte B**
11. **Posto**: «Sposta su un'altra macchina…» c'è 1 volta in Altro ▾ per l'admin della macchina, 0 per l'admin del
    workspace; 0 pulsanti nuovi sulla mappa e nella lista Workspace.
12. **Scelta**: tanti `input[type=radio]` quanti collegamenti HTTPS/SSH della macchina dell'istanza; ogni radio
    `disabled` ha uno `span[title]` o un testo accanto non vuoto; ogni riga contiene «95° percentile» e «liberi».
13. **Fermo**: database dedicato, sonda ogni 100 ms: tempo senza 200 ≤ 90 s, 0 risposte 502; il deploy dell'istanza ha
    la riga del fermo.
14. **Ritorno**: Torna su lab3 riporta l'istanza con una riga scritta su lab4 dopo lo spostamento (presente su lab3
    dopo il ritorno, 3 volte su 3).
15. **Cliente**: l'utente del workspace, prima e dopo, entra dallo stesso indirizzo e vede la stessa istanza; 0
    occorrenze di «lab3»/«lab4» nella sua pagina e nei suoi job.

**Comuni**
16. Ogni `button`, `summary`, radio, casella e voce di menu nuovi ha un `title` non vuoto e diverso dal testo; 0
    chiamate ad `alert`, `confirm`, `prompt`; ogni data `\d{2}/\d{2}/\d{4} \d{2}:\d{2}`.
17. A 375 px `scrollWidth` ≤ 375 in ogni dialogo e banner nuovi; ogni pulsante alto almeno 44 px.

## Smell che fanno FAIL

- Un nome di macchina, un IP o una cartella del fornitore nel pannello del cliente o nei suoi job.
- Un passaggio che ferma le istanze senza nome scritto, o che lascia una produzione su due macchine che scrivono.
- Un fermo detto a parole e non misurato nel job.
- Un dato del cliente che resta da noi oltre la data scritta nel riepilogo.
- Un secondo `.primary` nella stessa schermata o nello stesso passo.
- Un'azione di consegna o di spostamento nella lista Workspace o sulla mappa.
- «Non passa» senza il perché (passkey, token, OAuth, staging).
- Gli screenshot presi solo come admin della macchina, o con il gruppo developer.

## Decisioni per Carlo (la raccomandata per prima)

Esempio comune: Cliente1, 2 produzioni (erp: database 1,8 GB e file 1,2 GB; shop: 0,5 GB), 1 staging, 6 utenti (1 con
passkey, 3 con verifica in due passaggi), 2 token, 1 accesso OAuth, 40 backup locali da 1,5 GB (60 GB), linea 22 MB/s.

- **D-A0 — Dove sta «Consegna al cliente…».**
  - **Raccomandata: Limiti e disco › Dove vive**, 2 clic dalla pagina del workspace, accanto a Elimina il workspace…:
    le azioni rare e definitive dell'admin della macchina stanno lì.
  - Alternativa: nella testa della pagina del workspace: 1 clic, ma la testa passa da 2 a 3 elementi per un'azione che
    si fa una volta.
- **D-A1 — Chi prepara.**
  - **Raccomandata: solo l'admin della macchina** (il fornitore decide quando un cliente esce: contratto, fatture).
  - Alternativa: anche l'admin del workspace, in autonomia: il cliente se ne va senza passare da noi; per Cliente1
    vuol dire 6,1 GB che escono senza che Carlo lo sappia prima, solo dal registro.
- **D-A2 — Il canale.**
  - **Raccomandata: la macchina del cliente chiama la nostra in HTTPS con il codice.** Lui non apre porte per ricevere;
    copia 6,1 GB in circa 5 minuti mentre tutto gira, poi fermo di circa 3 minuti.
  - Alternativa: un file del pacchetto scaricato e caricato: niente rete fra le due, ma il fermo è tutto il viaggio
    (scarica, carica, ripristina): circa 15 minuti per Cliente1.
  - Alternativa: un collegamento fra le due macchine: lascia un legame che il cliente non vuole («niente di nostro»).
- **D-A3 — Cosa teniamo dopo il passaggio (GDPR).**
  - **Raccomandata: copia ferma e backup per 7 giorni, poi tutto cancellato**: 4,1 GB di disco + 60 GB di backup per 7
    giorni; il registro degli accessi resta, per la sua conservazione.
  - Alternativa: 30 giorni: lo stesso disco per 30 giorni, e serve un accordo scritto con il cliente sul trattamento.
  - Alternativa: cancellazione al passaggio: 0 GB da noi, ma se la macchina del cliente muore il giorno dopo resta solo
    il suo ultimo backup.
- **D-A4 — Chi passa.**
  - **Raccomandata: il cliente, dalla sua macchina**, nella finestra che sceglie lui (il nome scritto di Carlo in
    Prepara è il consenso del fornitore). Carlo non deve essere sveglio alle 22:00.
  - Alternativa: Carlo, da noi, con il nome scritto: ogni consegna aspetta un appuntamento con lui.
- **D-A5 — Password e verifica in due passaggi.**
  - **Raccomandata: passano gli hash e i segreti TOTP**: 6 utenti su 6 entrano il giorno dopo con la stessa password e
    la stessa app; 1 riregistra la passkey.
  - Alternativa: non passano: 6 inviti da mandare e 3 app da riconfigurare, ma nessun segreto lascia la nostra macchina.
- **D-A6 — Token e accessi OAuth.**
  - **Raccomandata: non passano, e il dialogo li elenca**: 2 token e 1 accesso da rifare là (i client devono comunque
    cambiare indirizzo).
  - Alternativa: passano i token: i 2 client cambiano solo l'indirizzo, ma un token nato per il nostro pannello vale su
    un altro.
- **D-A7 — Backup locali.**
  - **Raccomandata: l'ultimo di ogni ambiente**: 4 backup, 2,4 GB, circa 2 minuti in più; i 36 restanti (57 GB) si
    cancellano con la copia. Le copie nelle destinazioni del workspace restano dove sono e passano con le chiavi.
  - Alternativa: tutti: 60 GB, circa 45 minuti in più di ricezione.
  - Alternativa: nessuno: il primo backup là lo fa la pianificazione della notte.
- **D-A8 — Registro degli accessi.**
  - **Raccomandata: passa un archivio delle righe di Cliente1** (4.210 righe), e il nostro registro resta intero da noi.
  - Alternativa: niente: il suo registro NIS 2 comincia il giorno del passaggio.
- **D-A9 — Nomi che puntano ancora a noi.**
  - **Raccomandata: la nostra macchina inoltra alla sua finché il DNS non punta, al più per i 7 giorni della copia.**
    Con un TTL di 3600 s, senza inoltro, fino a 1 ora di pagina di manutenzione per chi ha il DNS vecchio.
  - Alternativa: nessun inoltro: l'avviso del TTL diventa obbligatorio un giorno prima.
- **D-A10 — L'avviso agli utenti del cliente.**
  - **Raccomandata: facoltativo, spento di default**, con data e ora scritte da Carlo: è il cliente che avvisa la sua
    gente.
  - Alternativa: sempre acceso dal momento di Prepara: 6 utenti lo leggono anche se la consegna non si fa più.
- **D-B1 — Dove sta «Sposta su un'altra macchina…».**
  - **Raccomandata: Altro ▾ della pagina dell'istanza**: 2 clic dall'istanza che pesa.
  - Alternativa: una colonna «Sposta» sulla pagina della macchina carica: 1 clic per istanza, ma un pulsante in più per
    ogni riga (con 12 istanze, 12 pulsanti).
- **D-B2 — Il cliente dell'istanza spostata.**
  - **Raccomandata: il workspace resta a casa sua (lab1), che opera l'istanza su lab2 per lui**: stesso portale, stessi
    6 utenti, 0 cose da rifare. Serve un AD, oggi non esiste.
  - Alternativa: la parte B solo per istanze senza utenti del cliente (interne): si costruisce subito, ma per Cliente1
    non vale.
  - Alternativa: si sposta il workspace intero con la parte A verso una nostra macchina: il portale cambia, 1 passkey
    da rifare e 2 token da rifare.
- **D-B3 — I nomi dopo lo spostamento.**
  - **Raccomandata: si cambiano i record DNS, con lab1 che inoltra finché non puntano.** 3 record da cambiare; lab1
    smette di servire acme-erp.
  - Alternativa: lab1 resta il davanti (come un relay): 0 record, ma lab1 continua a portare tutto il traffico di
    acme-erp, e una sua caduta ferma anche lei.
- **D-B4 — Quanto tiene la copia ferma.**
  - **Raccomandata: 7 giorni**: 3,3 GB su lab1 per una settimana; Torna su lab1 copia solo le modifiche.
  - Alternativa: 3 giorni: un difetto che esce il lunedì dopo uno spostamento del venerdì trova la copia cancellata.
  - Alternativa: 14 giorni: 3,3 GB per due settimane su una macchina che si voleva alleggerire.
- **D-B5 — Le staging della produzione.**
  - **Raccomandata: restano dove sono e seguono la produzione nuova**: 0 GB in più da copiare; Ricostruisci le rifà
    dalla produzione su lab2.
  - Alternativa: vanno con lei: 2,0 GB in più per staging, e lab1 si alleggerisce di più.
- **D-C — Spostare un workspace intero fra due macchine nostre.**
  - **Raccomandata: nessun flusso suo**: è la parte B per ogni istanza (il portale resta) o la parte A verso una
    macchina nostra (il portale cambia).
  - Alternativa: un flusso dedicato «Sposta il workspace…»: una schermata in più con le stesse domande della parte A.

## Decise da Carlo (23/11/2026)

D-A3 copia ferma e backup per 7 giorni, poi cancellati; D-A4 il passaggio lo preme il cliente; D-A5 passano gli hash
delle password e i segreti TOTP; D-B2 l'istanza spostata resta gestita dal workspace a casa sua (serve l'AD). Le altre
decisioni seguono la raccomandata finché Carlo non dice altro.

## Non verificato

- Il consumo **per istanza** sui 14 giorni: il monitoraggio di oggi è della macchina. Senza, §B4 riga 1 dice solo
  «adesso».
- I tempi di fermo: 10 s per il database dedicato sono misurati (UX-011, 22/11); l'avvio di Odoo e il passaggio dei
  nomi in un passaggio vero no. I tempi del ripristino sono stime da misurare su lab3/lab4.
- Se il cliente può raggiungere il nostro pannello: dipende da `portal_address` o da un nome pubblico di ogni nostra
  macchina; lab1 e lab2 oggi sono su indirizzi privati.
- Una staging su una terza macchina che deve seguire una produzione spostata (D-B5): oggi la staging ricorda la
  macchina della produzione attraverso i collegamenti di quella macchina.
- Le passkey: il RP ID è il nome del pannello (`passkeys.py`); se il nostro portale e quello del cliente stanno sotto
  lo stesso dominio registrabile il comportamento cambia. Il contratto assume che non passino.

## Tracciabilità

Richieste di Carlo del 23/11/2026 in chat; non ci sono un `TS-NNN` né un `T<n>`/`HT<n>`.
- UX-007 §4.4 (pagina e modulo del workspace), UX-008 §2 e §5.1 (staging su un'altra macchina), UX-009 §6-7, UX-010 §2,
  UX-011 §A e §D (prepara, completa, annulla; replica di un dedicato), UX-013 §A2, §A6, §A9.
- Regole di Carlo: produzione solo a mano (22/11), REST e MCP per ogni capacità (22/11), NIS 2 per ogni azione (21/11),
  regola 9 (scelte con il caso in numeri).

Fonti nel codice e nel README:
- `/home/carlo/Projects/OmniaCLI/README.md`: Workspaces (1240), Domains of a workspace (1708), Published through another
  machine (1775), Stagings (2178), A database on another machine (2636), A read replica of a dedicated database (2883),
  Full archive (3165);
- `/home/carlo/Projects/OmniaCLI/omniacli/`: `instancearchive.py`, `linkcopy.py`, `managedreplica.py`, `zerodowntime.py`,
  `relay.py`, `deploys.py`, `github.py`, `oauth.py` (emittente = indirizzo del pannello), `passkeys.py` (RP ID),
  `destinations.py`, `schedules.py`, `accesslog.py`, `metricshistory.py`, `config.py` (`portal_address`,
  `provider_label`);
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/`: `workspace.html` (testa 57-80), `workspaces.html` (Elimina, 66 e
  170), `server.html` (Altro ▾, 225), `job.html` (passi, 96-130), `base.html` (`omniaTypeName`, 835-880);
- commit `dcf5cab` (pagina di manutenzione), `8bd15ad` (relay), `0d38571` e `6fb5432` (staging su un'altra macchina).
