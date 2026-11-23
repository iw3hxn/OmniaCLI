# UX-016 — Ogni backup va da qualche parte: la destinazione di ogni istanza, e quella predefinita del server

Stato: contratto di design, **seconda versione**, prima della build. Sostituisce la prima (commit fff6bbf), che proponeva un archivio unico per tutte le macchine: Carlo il 23/11/2026 ha scelto una destinazione per istanza e per server (§20). File: `/home/carlo/Projects/OmniaCLI/docs/ux/UX-016-archivio-dei-backup.md` (il nome del file resta; il titolo cambia).
Lo implementa chi scrive `omniacli/templates/destinations.html` (la predefinita del server), la scheda Backup di `server.html` («Copia fuori sede in», la lista, «Copie in …»), `_backup_dialog.html` (Backup adesso), `backups.html` (lista, «Carica un backup…», «Ripristina in un'istanza nuova…») e le route di `omniacli/app.py` intorno a `/api/backups`, `destinations`, `imports/upload`, `instances/from-backup`, e la sorveglianza della mappa (`inframap`, la campanella). Jinja e JS senza framework, classi esistenti: `.panel`, `.badge`, `.small`, `.muted`, `.mono`, `.note-box`, `details.row-menu`, `dialog`, `.hide-sm`. Le stringhe vanno in `omniacli/i18n/it/*.json`.
Valgono le regole comuni e i criteri di UX-001…015, in particolare UX-006 §2-3, UX-008 §2 regole 1-4 e §4.2 (la pagina Backup), UX-009 §6 e la sorveglianza della mappa (campanella, «N da guardare»). Questo contratto li applica e non li ripete.

Origine: Carlo, 23/11/2026, sul dialogo «Carica un backup» (5502): «i backup dovrebbero essere su una cartella unica, magari sul server locale, perché in questo modo si ha la protezione dei backup, che se dovesse andare giù il server i backup restano, oppure su quelle cartelle che si configurano dove si vogliono i backup, tipo FTP, Dropbox etc.». Poi, sulla prima versione: «va in base ad ogni istanza/server, perché ogni volta che faccio il backup deve andare da qualche parte».

**Prima della build serve un AD** (`software-architect`, AD-016): chi conosce le destinazioni di una macchina quando la macchina è ferma, come si copia ogni tipo di backup senza rallentare un aggiornamento, chi sposta i byte di un ripristino su un'altra macchina. Le domande sono in §12.

---

## 0. Gate

UI utente non banale: **sì**. Cambia la scelta della destinazione (pagina Destinazioni della macchina, scheda Backup dell'istanza), la lista quotidiana dei backup, i dialoghi di caricamento e di ripristino. Rischio-accrescimento: una seconda lista «copie fuori sede», una colonna per destinazione, un'impostazione in più per ogni livello (macchina, workspace, istanza, tipo di backup). Il contratto tiene **due livelli** (server e istanza), **una riga per backup** e **un solo campo** per scegliere.

**Le parole.** «Server» è la macchina, come nella colonna Server e nel menu (UX-001: Server, Istanza, Ambienti). «Fuori sede» è la copia fuori dalla macchina, ovunque sia. «Destinazione» è il nome di oggi delle impostazioni.

## 1. Cosa c'è oggi (23/11/2026, `carlo_dev_2026`, fff6bbf, rilascio 5502)

| Fatto | Dove sta oggi | Difetto rispetto alla richiesta |
|---|---|---|
| Destinazioni | Impostazioni › Destinazioni dei backup della macchina (`/destinations`, admin) e Workspace › Servizi esterni › Destinazioni (admin del workspace). rclone; ogni copia cifrata con la chiave della destinazione (`.ocpf`); impostazioni e chiave in `destinations.yaml` **della macchina** | nessuna è «quella del server»: una macchina non ha un valore predefinito |
| Copia fuori sede | scheda Backup dell'istanza › Backup automatici › «Copia anche in»: una destinazione o «da nessuna parte: solo questa macchina» (il valore iniziale); stessa rotazione della macchina | spenta finché non si sceglie, istanza per istanza; **solo i backup automatici** vanno fuori: «Backup adesso», «Backup prima di un aggiornamento» e i caricati restano solo sulla macchina |
| Copie nella destinazione | scheda Backup dell'istanza › `details` «Copie in <destinazione>», con «Riporta qui» | solo se la macchina risponde; la pagina Backup globale non le vede |
| Pagina Backup globale | i backup di ogni macchina, una riga per file; Istanza · Server · Fatto il · Dimensione · Esito · Azioni ▾ | non dice se un backup ha una copia fuori sede; una macchina giù è «i suoi backup qui non ci sono» |
| «Carica un backup…» | Server → Workspace → File; nell'area di import del workspace sulla macchina, 7 giorni | solo sulla macchina |
| Ripristina in un'istanza nuova… | sulla macchina del backup | da un backup di lab1 non si fa un'istanza su lab2 |
| Prova di ripristino | la domenica, sul backup della macchina | nessuna prova che una copia fuori sede si decifri e si ripristini |
| Pannello centrale | l'OmniaCLI di Carlo (localhost:8090, sul suo PC), collegamenti **in uscita** verso lab1, lab2, ber, inaria, odoo-test, ridix | non conosce le destinazioni delle macchine: se lab1 muore, nessun pannello sa dove sono le sue copie né ha la chiave per leggerle |

## 2. Persone e lavoro primario

| Persona | Chi è nel codice | Cosa fa qui |
|---|---|---|
| **P1 Integratore sul centrale** (Carlo) | `is_admin`, non `scoped`, `listed_links` non vuoto | sceglie la destinazione predefinita di ogni server, guarda che nessuna produzione resti senza copia fuori sede, ripristina su un'altra macchina quando una è giù |
| **P1b Admin del server, direttamente** (sul pannello di lab1) | `is_admin`, non `scoped` | come P1, per il suo server |
| **P2 Admin del workspace** (il cliente) | `scoped`, admin nel workspace | sceglie per la sua istanza una destinazione del suo workspace al posto di quella del server; vede se i suoi backup hanno la copia fuori sede; carica un backup. **Nessun nome di macchina, del centrale, né il nome o il percorso delle destinazioni del server** (`customer_view`) |
| **P3 Sviluppatore** | ruolo `developer` | legge e scarica (UX-008 §3) |
| P4 Utente | ruolo `user` | la voce Backup non c'è |
| P5 Ruolo personalizzato | capacità `backup`, `download`, `restore`, `instances` | quello che le capacità permettono; il resto disattivato con il motivo |

Device: desktop 1440×900. Telefono 375×812 per leggere «stanotte è andata, la copia fuori sede c'è?» e per scaricare.

**Lavoro primario**: in 3 secondi, «ogni backup delle produzioni è anche fuori dalla macchina?», e se un server è giù, «dove sono i suoi backup, e li rimetto su un altro».
Secondario: scegliere la destinazione predefinita di un server e quella di un'istanza che fa eccezione; caricare un database; ripristinarlo su qualunque server.

## 3. Le regole: dove va un backup

1. **Ogni server ha una destinazione predefinita** (§4), e ogni sua istanza la usa se non ne sceglie un'altra.
2. **Un'istanza può sceglierne un'altra** (§5): una del server o, per P2, una del suo workspace. È l'unica eccezione possibile: per una produzione **non c'è l'opzione «da nessuna parte»** (D-N1).
3. **Ogni backup di una produzione va nella destinazione della sua istanza** (D-N2): automatico, «Backup adesso», «Backup prima di un aggiornamento», archivio completo. Il backup si fa prima sulla macchina; la copia fuori sede è il passo dopo, nello stesso job.
4. **Il backup sulla macchina non aspetta mai la copia e non si rifiuta mai per lei.** Senza destinazione, o con la copia non riuscita, il backup c'è lo stesso, e la cosa diventa **da guardare** (§6).
5. **Staging e test** non hanno una copia fuori sede e non sono mai «da guardare» per questo (D-N3): sono copie della produzione. Il `title` della loro riga lo dice.
6. **Si parte dalla notte dopo** (D2 di Carlo): scegliere o cambiare una destinazione non copia i backup che ci sono già. Il prossimo backup è il primo.
7. **Chi cambia destinazione non perde le copie di prima**: restano dove sono, con la loro rotazione, e si vedono nella lista finché ci sono.

## 4. La destinazione predefinita del server

**Dove**: nella pagina Destinazioni dei backup del server (Impostazioni › Destinazioni dei backup; dal centrale, «Impostazioni di lab1» › Destinazioni dei backup). È lì che le destinazioni nascono; le Opzioni della macchina parlano di posta e monitoraggio, e la pagina del server non ha impostazioni.

- La tabella delle destinazioni del server ha una pastiglia **«Predefinita»** sulla destinazione scelta (al massimo una) e, sulle altre, l'azione **«Rendi predefinita…»** (`button small`, secondario). Il dialogo dice cosa cambia, con i numeri: «Le 4 produzioni di lab1 che usano la predefinita copieranno qui dal prossimo backup (stanotte alle 02:00). Le 2 che hanno scelto un'altra destinazione non cambiano. Le copie già fatte restano dove sono.» Primario del dialogo «Rendi predefinita»; Annulla.
- **La chiave, prima**: se la chiave di quella destinazione non è mai stata mostrata, il dialogo la mostra e chiede «Ho salvato la chiave fuori da qui» prima del primario (senza chiave le copie non si leggono altrove; mostrarla è già una riga del registro).
- **Nessuna predefinita**: sopra la tabella, una `note-box` in `--warn`: «Nessuna destinazione predefinita: i backup delle 4 produzioni di questo server che non ne scelgono una restano solo qui. Se il server si ferma, si fermano con lui.» Con 0 destinazioni, il link «+ Nuova destinazione» è nel testo.
- La pagina è degli admin del server (P1, P1b). P2 ha la sua pagina Destinazioni del workspace, senza predefinita (D-N4).
- Togliere la predefinita («Non è più predefinita…», `danger`): le produzioni che la usavano diventano da guardare; il dialogo dice quante.
- Una destinazione predefinita, o scelta da un'istanza, non si elimina (oggi: «una destinazione in uso non si elimina»).

## 5. La destinazione di un'istanza (scheda Backup)

Il campo «Copia anche in» di oggi diventa **«Copia fuori sede in»** e si sposta **fuori** dal `details` «Backup automatici», perché vale per ogni backup, non solo per gli automatici: una riga sotto il titolo della scheda, accanto allo stato dei backup automatici.

- **Le opzioni**, in quest'ordine:
  1. «Predefinita del server: wasabi» (P2: «Predefinita del server»), il valore iniziale di ogni istanza;
  2. le altre destinazioni del server (solo P1, P1b);
  3. le destinazioni del workspace dell'istanza («del workspace: dropbox-acme»).
- **Nessuna opzione «da nessuna parte»** per una produzione (D-N1). Per staging e test il campo non c'è (D-N3).
- **Il server non ha una predefinita e l'istanza nessuna scelta**: il campo mostra «Nessuna destinazione» in `--warn`, con accanto «I backup restano solo su questa macchina.» e, per P1, il link «Scegli la predefinita del server ›»; per P2 «Aggiungi una destinazione del workspace ›».
- Salvare il campo non avvia niente: la riga sotto dice «Dal prossimo backup.» (regola 6).
- La rotazione resta quella di oggi nel `details` «Backup automatici» e vale anche fuori sede (D3).
- **Backup adesso**: il dialogo ha una riga in `.small`: «Va anche in wasabi, cifrato.» (P2: «Va anche fuori sede, cifrato.»); senza destinazione: «Resta solo su questa macchina: nessuna destinazione.» in `--warn`. Nessun campo in più.
- La lista dei backup della scheda prende la colonna **Dove** di §7; il `details` «Copie in <destinazione>» sparisce, e una copia che c'è solo fuori sede è una riga con «Riporta qui» al posto di Scarica.

## 6. «Ogni backup va da qualche parte»: quando non ci va

Raccomandato (D-N1): **il backup si fa, il job lo dice, e la produzione diventa da guardare**. Mai un rifiuto: un backup solo sulla macchina vale più di nessun backup, e il «Backup prima di un aggiornamento» bloccherebbe l'aggiornamento.

| Caso | Il job | Da guardare (campanella, «N da guardare», mappa) | La pagina Backup |
|---|---|---|---|
| Produzione senza destinazione | finisce **«⚠ con avvisi»**: «Nessuna copia fuori sede: acme-odoo non ha una destinazione e il server non ha una predefinita.» | una voce per produzione, «acme-odoo: i backup restano solo sulla macchina», finché non ha una destinazione | la riga ha solo «Macchina ✓»; il filtro della testa la conta |
| Copia non riuscita (destinazione piena, irraggiungibile, chiave sbagliata) | il backup automatico fallisce come oggi e «Se fallisce, avvisa» parte; gli altri tipi finiscono «⚠ con avvisi» | una voce col motivo, finché la copia seguente non riesce | riga fallita «Copia fuori sede non riuscita: <motivo>», link al job; il backup è la riga sotto, con «Macchina ✓» |
| Staging, test | nessun avviso | niente | `title` «Le staging sono copie della produzione: non hanno copia fuori sede» |

«Se fallisce, avvisa» non si manda per la sola mancanza di destinazione: arriverebbe ogni notte con lo stesso testo. La campanella la dice una volta, finché non è risolta.

## 7. La pagina Backup

**Testa**: «Backup», secondario **Carica un backup…** (nessun primario; UX-008 §2 r1).
**Riga dei conteggi**: com'è oggi più un pezzo, «… · **3 produzioni con l'ultimo backup solo sulla macchina**». È un filtro (UX-008 §2 r3) che mostra l'ultimo backup di quelle 3; il conteggio dell'elenco filtrato è 3; con 0 il pezzo non c'è. Il `title` dice i due motivi possibili (nessuna destinazione, copia non riuscita); la riga di ognuna dice il suo.

**Una riga per backup**, non una per copia: il file della macchina e la sua copia fuori sede sono la stessa riga (server, ruolo, nome del file, SHA-256).
Colonne: Istanza · Server (con più macchine) · Fatto il · Dimensione · **Dove** · Esito · Azioni ▾.

**Dove**, pastiglie in ordine fisso, con parola e segno, mai solo colore:
- «Macchina ✓»: il file è sulla macchina. `title`: «Sulla macchina lab1, SHA-256 scritto il 23/11/2026 03:02» (P2: «Sulla macchina»).
- «<destinazione> ✓» per P1/P1b («wasabi ✓», «dropbox-acme ✓»); «Fuori sede ✓» per P2 quando la destinazione è del server, il nome quando è del suo workspace. `title`: «wasabi · S3 · copiata il 23/11/2026 03:41 · cifrata»; dalla prima domenica del mese in più «ripristino provato da questa copia il 07/12/2026» (D6 di Carlo).
- Una riga può avere due pastiglie fuori sede quando un'istanza ha cambiato destinazione e la rotazione di prima tiene ancora il backup nella vecchia: si vedono entrambe.
- **Pastiglia assente** = il backup lì non c'è. Mai «✗»: un backup di prima della destinazione non è un allarme (regola 6).
- «Macchina ?» in `--muted` quando la macchina non risponde.
- «<destinazione> …» mentre la copia gira, `title` «Copia in corso», link al job.

L'**Esito** resta la prova di ripristino del backup (stesso SHA-256 per tutte le copie).

**Una macchina che non risponde** (lo scopo di tutto):
- la sua riga diventa, se il centrale conosce le sue destinazioni (D-N5): «**lab1 non risponde** (<motivo>): qui ci sono le sue 46 copie fuori sede, lette da wasabi e dropbox-acme. Si ripristinano su un altro server.»; altrimenti il testo di oggi più «Le sue copie sono nelle sue destinazioni: aggiungine una qui con la sua chiave per vederle.» (link, P1);
- le sue copie sono righe con «Macchina ?» e la pastiglia della destinazione; menu: **Scarica** (decifrata dal centrale mentre arriva) e **Ripristina in un'istanza nuova…**; niente Elimina;
- le copie di una macchina che non risponde non ruotano (nessun backup automatico le fa uscire): il `title` della riga della macchina lo dice.

## 8. «Ripristina in un'istanza nuova…» da una copia fuori sede

Il dialogo di 5502 resta; cambia dove può andare.
1. **Server** (primo campo nuovo; solo P1/P1b, solo con 2 o più server che rispondono e hanno un template della versione di Odoo del backup): predefinito il server del backup se risponde, altrimenti il primo adatto. Gli altri sono opzioni disattivate con il motivo («ber · nessun template di Odoo 18», «lab1 · non risponde»). P2 non lo vede.
2. **Cosa diventa**: «Una staging di <produzione>» o «Un'istanza nuova». La staging solo sul server della produzione: con quel server giù è disattivata con «La produzione è su lab1, che non risponde: fanne un'istanza nuova su un altro server.».
3. **Il piano** arriva senza aspettare il file: versione di Odoo, database e moduli installati sono letti quando il backup è copiato fuori sede, e salvati accanto alla copia, cifrati come lei. La riga dello spazio dice anche il trasferimento: «Il file arriva da wasabi: circa 6 GB, una decina di minuti.»
4. **Crea l'istanza** avvia il job; il primo passo è «Porta il backup da wasabi» (avanzamento, ripresa), poi quelli di oggi. Annulla: nessuna richiesta.
- Sulla stessa macchina, con il file già uscito dalla rotazione locale, il percorso è lo stesso: il job lo riporta prima.

## 9. «Carica un backup…»: sulla macchina scelta, e fuori sede nella sua predefinita

Con una destinazione per server, il file si carica **dove si ripristinerà**, e da lì va anche fuori sede, come ogni altro backup.

1. **Server** (come oggi; P1, e solo con 2 o più server che accettano il caricamento), poi **Workspace** (P2: il suo, e il campo non c'è se ne ha uno), poi **File**.
2. Il testo, riscritto: «… fino a 50 GB. **Resta 30 giorni su questo server e una copia cifrata va in wasabi, la sua destinazione predefinita.** Dalla sua riga fai una staging o un'istanza nuova, anche su un altro server.» (P2: «… una copia cifrata va fuori sede.»)
3. **Server senza predefinita**: la stessa riga dice in `--warn` «Nessuna destinazione predefinita su lab1: il file resta solo qui, 30 giorni.» Il caricamento non si blocca. Il file non è «da guardare»: non è il backup di una produzione.
4. Fine: «Caricato: acme.zip · Odoo 18.0 · database acme · con allegati · 2,4 GB. Resta fino al 23/12/2026; la copia in wasabi è in corso.» e il primario «Fai una staging o un'istanza…» (come oggi).
5. La riga nella lista: pastiglia «Caricato» nell'Istanza, Dove «Macchina ✓ · wasabi ✓»; Elimina… toglie tutte e due (un caricato non ha una rotazione da proteggere: è il file di chi l'ha caricato).
- Scadenza: **30 giorni** (D4 di Carlo) sulla macchina e fuori sede; un caricamento interrotto resta 2 giorni come oggi.
- Spazio, prima di inviare: la macchina come oggi; la destinazione dove dice lo spazio libero (§Non verificato). Pieno: un errore col rimedio, il file resta sulla macchina.

## 10. Il necessario

- **Stato del sistema**: pastiglie «Macchina», «<destinazione>»/«Fuori sede» con parola e segno; «…» durante la copia; il job «⚠ con avvisi» senza destinazione; la voce della campanella; la `note-box` «Nessuna destinazione predefinita»; la riga della macchina giù con il numero delle copie; il filtro della testa.
- **Annulla**: ogni dialogo ha Annulla che non manda richieste; un caricamento si ferma e riprende; cambiare destinazione o togliere la predefinita non cancella copie.
- **Prevenzione dell'errore**:
  - nessuna opzione «da nessuna parte» per una produzione;
  - la chiave mostrata e confermata prima di rendere predefinita una destinazione;
  - «Elimina…» su un backup cancella il file della macchina; la copia fuori sede solo con la casella «anche la copia in wasabi (non si recupera)», spenta di default e solo per P1/P1b (eccezione: un caricato, §9.5);
  - niente Elimina sulle righe di una macchina che non risponde;
  - nel ripristino, server senza template o che non risponde disattivati con il motivo.
- **Messaggio d'errore** (tradotto, con il rimedio):
  - «wasabi è piena (998 di 1000 GB): libera spazio o accorcia la rotazione delle produzioni più grandi.»
  - «La copia non si decifra con la chiave di wasabi: le copie sono state fatte con un'altra chiave. Dai alla destinazione la chiave di prima (Impostazioni › Destinazioni dei backup).»
  - «wasabi non risponde (<motivo>): le sue copie qui non ci sono. I backup sulle macchine sì.» (una riga, come una macchina muta).
- **Aiuto**: `title` su ogni pastiglia, pulsante, opzione e voce di menu; la riga «Dal prossimo backup.»; le `note-box` dicono la conseguenza con i numeri.

## 11. Contratto dei dati (i nomi definitivi li decide chi implementa)

- **La predefinita del server**: un campo in `destinations.yaml` (una sola). La destinazione di un'istanza: `BackupStep.destination` di oggi, con un valore che vuol dire «la predefinita» distinto da una scelta esplicita, così cambiare la predefinita sposta le istanze che la seguono e non le altre.
- **Ogni tipo di backup** passa dalla copia: automatico (come oggi), manuale, prima di un aggiornamento, archivio, caricato. Il passo «Copia fuori sede» nel job, dopo il backup.
- `/api/backups` aggiunge a ogni backup `places`: `{machine: {present, sha256_time}, offsite: [{destination, label, workspace, copied, tried}]}`. Per P2 i nomi delle destinazioni del server non ci sono (`customerview`).
- Ogni macchina legge le sue destinazioni **una volta per destinazione** (elenco ricorsivo, cache 10 minuti), non una per istanza.
- **La ricevuta** della copia nel `.omniacli-backups.json` della macchina (`copied`, `destination`).
- **I fatti della copia** (versione di Odoo, database, allegati, moduli) accanto alla copia, cifrati.
- La sorveglianza (`inframap`, campanella): un tipo «produzione senza copia fuori sede» e uno «copia fuori sede non riuscita», con la produzione come nodo.
- Registro degli accessi (NIS 2): predefinita scelta e tolta, destinazione di un'istanza cambiata, chiave mostrata, caricamento, scarico da una destinazione, eliminazione di una copia fuori sede.

## 12. Domande per `software-architect` (AD-016, prima della build)

1. **Chi sa dove sono le copie di una macchina ferma** (D-N5): il centrale legge attraverso il collegamento (admin) le destinazioni di ogni macchina collegata, chiavi comprese, e le tiene sigillate? Con quale frequenza, e cosa succede quando le chiavi cambiano. Rete per quando il centrale non le ha: aggiungere la destinazione a mano con la sua chiave.
2. **La copia del «Backup prima di un aggiornamento»**: l'aggiornamento parte appena c'è il backup sulla macchina, e la copia va dopo (nello stesso job o in un job suo)? 6 GB a 20 MB/s sono 5 minuti di attesa in più per ogni aggiornamento se la copia va prima.
3. **Il ripristino su un'altra macchina**: la macchina che riceve legge la destinazione con le impostazioni e la chiave che le passa il centrale per quel job, oppure il centrale scarica, decifra e carica a parti nell'area di import (la route di 5502)?
4. **La prova della prima domenica del mese** dalla copia fuori sede: dove si scarica e si ripristina, con quale spazio, e se un errore di decifratura fallisce il giro come oggi.
5. **Le destinazioni del workspace** scelte da P2: il centrale le conosce (domanda 1) o il ripristino da quelle, a macchina ferma, chiede la chiave al cliente?

## 13. Visibilità

| Elemento | P1 centrale | P1b admin server | P2 admin workspace | P3 sviluppatore | P5 ruolo personalizzato | Meccanismo |
|---|---|---|---|---|---|---|
| Destinazioni del server, «Predefinita», «Rendi predefinita…» | sì (per ogni server, da «Impostazioni di …») | sì | — | — | — | `Depends(admin)` |
| `note-box` «Nessuna destinazione predefinita» | sì | sì | — | — | — | `is_admin`, non `scoped` |
| Destinazioni del workspace | sì | sì | sì, le sue | — | — | admin del workspace |
| Campo «Copia fuori sede in» (produzione) | tutte le opzioni | tutte | predefinita (senza nome) + quelle del workspace | sola lettura | con `backup`, disattivato senza | `can_act`, `denied.backup` |
| Riga del dialogo Backup adesso | col nome | col nome | «fuori sede» | col suo ambito | con `backup` | `customer_view` |
| Righe dei backup | ogni server | il suo + collegamenti | le sue istanze | come il suo ambito | le istanze a cui è limitato | `visible_servers` |
| Colonna Dove | nomi delle destinazioni | idem | «Fuori sede» per quelle del server, il nome per quelle del workspace | come il suo ambito | sì | `customer_view` |
| Righe di una macchina giù, dalle destinazioni | sì | — | — | — | — | centrale (D-N5) |
| Filtro «N produzioni … solo sulla macchina» | sì | sì | sì, le sue | sì | con `backup` | filtro |
| Voce della campanella | sì | sì | sì, le sue | — | — | sorveglianza della mappa |
| Carica un backup… | sì | sì | sì, nel suo workspace | — | con `instances` | `upload_here`, `upload_links` |
| Campo Server del caricamento e del ripristino | sì, con 2+ server adatti | idem | — | — | — | `is_admin`, non `scoped` |
| Scarica (anche da una destinazione) | sì | sì | sì | sì | con `download` | `download_refused` |
| Casella «anche la copia in <destinazione>» di Elimina | sì | sì | — | — | — | `is_admin`, non `scoped` |
| Nomi di macchine, del centrale, delle destinazioni del server, percorsi | sì | sì | **mai** (DOM e JSON) | come il suo `scoped` | idem | `customer_view` |

## 14. HIDE / DEFER

| Cosa | Dove va | Rete di sicurezza |
|---|---|---|
| Una lista o pagina «Copie fuori sede» | la colonna Dove della stessa riga | — |
| Una colonna per destinazione | una colonna Dove con le pastiglie | — |
| `details` «Copie in <destinazione>» della scheda Backup | le righe della lista della scheda, con «Riporta qui» | — |
| Una destinazione predefinita del workspace | — (D-N4) | P2 sceglie per le sue istanze |
| Un'impostazione per tipo di backup (automatico sì, manuale no…) | — (D-N2: tutti) | — |
| Opzione «da nessuna parte» per una produzione | — (D-N1) | il backup sulla macchina si fa sempre |
| Percorso, SHA-256, cifratura | `title` della pastiglia | — |
| Spazio di una destinazione | pagina Destinazioni | in testa alla pagina Backup oltre l'85%, solo P1/P1b |
| Campo Server del caricamento e del ripristino | solo con 2+ server adatti, solo P1/P1b | — |
| Filtro della testa | non c'è con 0 | — |
| Elimina sulle righe di una macchina giù | non c'è | torna quando risponde |

## 15. Vuoto, attesa, dati densi

- **Destinazione appena scelta**: le righe di prima restano con «Macchina ✓» soltanto; la riga «Dal prossimo backup (stanotte alle 02:00).» sotto il campo; il filtro della testa conta quella produzione finché il primo backup non è copiato.
- **Nessun backup**: il testo di oggi.
- **Attesa**: le righe delle macchine arrivano come oggi; le pastiglie fuori sede vengono con la risposta della macchina (che legge le destinazioni dalla sua cache), «…» al loro posto finché non ci sono. **Prime righe in < 2 s**; una destinazione lenta entro 5 s o la sua riga «non risponde», senza bloccare il resto.
- **Dati densi**: ~20 istanze × fino a 23 backup automatici, più manuali e di prima dell'aggiornamento: ~500 righe, 50 per pagina; la colonna Dove ha 1-3 pastiglie corte.

## 16. Telefono (fino a 600 px, la soglia di oggi)

- Il blocco di oggi; le pastiglie Dove sulla riga dell'esito. Il campo «Copia fuori sede in» della scheda Backup a tutta larghezza.
- Nessuno scorrimento orizzontale; menu, voci e select alti almeno 44 px.

## 17. Pulsanti e campi (`title` scritto dalla route, in inglese con l'italiano negli i18n)

| Elemento | Tipo | Etichetta | `title` |
|---|---|---|---|
| Rendi predefinita… | secondario `.small` | Rendi predefinita… | «Le produzioni di questo server che non ne scelgono un'altra copiano qui ogni backup, cifrato, dal prossimo. Le copie già fatte restano dove sono.» |
| Non è più predefinita… | `.small danger` | Non è più predefinita… | «Le produzioni che la usavano restano senza copia fuori sede finché non ne scelgono una: diventano da guardare. Le copie fatte restano.» |
| Copia fuori sede in | `select` | Copia fuori sede in | «Dove va una copia cifrata di ogni backup di questa istanza: automatici, a mano, prima di un aggiornamento. Vale dal prossimo backup.» |
| filtro della testa | link | N produzioni con l'ultimo backup solo sulla macchina | «Mostra l'ultimo backup delle produzioni che non ha una copia fuori sede: nessuna destinazione, o la copia non è riuscita» |
| pastiglia Macchina | `span[title]` | Macchina ✓ / Macchina ? | «Sulla macchina <m>…» / «<m> non risponde: non si sa se il file c'è ancora» |
| pastiglia fuori sede | `span[title]` | <destinazione> ✓ / Fuori sede ✓ | «<destinazione> · <tipo> · copiata il … · cifrata» |
| Carica un backup… | secondario | Carica un backup… | «Manda un backup di Odoo o un dump SQL da questo computer a un server: resta lì 30 giorni, con una copia cifrata nella sua destinazione predefinita. Dalla sua riga fai una staging o un'istanza.» |
| Elimina… | voce `danger` | Elimina… | «Cancella il file da questa macchina. La copia fuori sede resta, a meno di spuntarla nella conferma. Non si annulla.» |
| Ripristina in un'istanza nuova… | voce | invariata | in più: «Da una copia fuori sede: su un server che scegli; il file arriva dalla destinazione nel job.» |

## 18. Criteri di accettazione (Playwright)

**Dove**: la build su **lab3/lab4** con il pannello locale dal worktree; l'aspetto finale sul centrale di Carlo con lab1/lab2 dopo il rilascio. **Mai su ber, ridix, inaria, odoo-test.**
**Dati**: due destinazioni SFTP di prova (container sulla rete `omnia-lab`): `sftp-a`, predefinita di lab3; `sftp-ws`, del workspace `acme` di lab3. Su lab3: produzione `acme-odoo` che segue la predefinita, produzione `acme-shop` che ha scelto `sftp-ws`, una staging. Su lab4: una produzione, **nessuna predefinita**. Un ruolo personalizzato senza `download`.
**Persone**: P1 (centrale locale), P1b (diretto su lab3), P2 (admin di `acme`), P3, P5. Nessun gruppo developer. 1440×900 e 375×812, IT, tema chiaro e scuro.

1. **Predefinita**: nella pagina Destinazioni di lab3, 1 pastiglia «Predefinita»; «Rendi predefinita…» su un'altra apre un dialogo il cui testo contiene i numeri «1» (produzioni che la seguono) e «1» (che non cambiano); il primario è `disabled` finché «Ho salvato la chiave» non è spuntata, se la chiave non era mai stata mostrata; Annulla → 0 POST. Su lab4: 1 `.note-box` che contiene «Nessuna destinazione predefinita».
2. **Campo dell'istanza**: su `acme-odoo` il select ha come prima opzione «Predefinita del server: sftp-a» ed è quella scelta; 0 opzioni «da nessuna parte»; per P2 la prima opzione è «Predefinita del server» (senza «sftp-a») e ci sono le sole destinazioni del workspace; sulla staging il campo non c'è (0 elementi).
3. **Ogni backup**: «Backup adesso» su `acme-odoo` → il job ha il passo «Copia fuori sede» e alla fine la riga ha «Macchina ✓» e «sftp-a ✓»; su `acme-shop` la pastiglia è «sftp-ws ✓»; un «Backup prima di un aggiornamento» (un Git pull con un commit) ha anche lui la pastiglia, e l'aggiornamento è partito prima della fine della copia (§12.2, se l'AD la mette dopo).
4. **Senza destinazione**: «Backup adesso» sulla produzione di lab4 → il job finisce «⚠ con avvisi» con il testo che contiene «Nessuna copia fuori sede»; la campanella ha 1 voce nuova per quella produzione; la riga ha 1 sola pastiglia; il filtro della testa vale 1 e, cliccato, mostra 1 riga.
5. **Copia non riuscita**: con `sftp-a` fermo, il backup automatico di `acme-odoo` → 1 riga fallita «Copia fuori sede non riuscita», la riga del backup con «Macchina ✓» soltanto, «Se fallisce, avvisa» chiamato 1 volta.
6. **Una riga per backup**: `tr[data-file]` = numero di backup distinti; nessun file ha due righe.
7. **Macchina giù**: con `docker stop lab3`, dal centrale entro 7 s la riga della macchina contiene «lab3 non risponde» e il numero delle copie; le righe di lab3 hanno «Macchina ?» e la pastiglia della destinazione; 0 «Elimina…» nei loro menu; «Scarica» dà un file con lo SHA-256 scritto alla creazione.
8. **Ripristino altrove**: con lab3 fermo, «Ripristina in un'istanza nuova…» su un backup di `acme-odoo`: il campo Server c'è, lab3 `disabled` con il motivo, lab4 scelto; la staging `disabled` con un motivo che contiene «non risponde»; il piano in < 3 s con 0 byte nell'area di import di lab4; dopo «Crea» il primo passo del job è «Porta il backup da sftp-a» e il job finisce con l'istanza su lab4 in funzione.
9. **Caricamento**: su lab3 il testo del dialogo contiene «30 giorni» e «sftp-a»; a fine caricamento la riga ha «Caricato», «Macchina ✓», «sftp-a ✓», e il `title` la data a +30 giorni; su lab4 il testo contiene «resta solo qui» e il pulsante Carica è attivo; 0 voci nuove nella campanella.
10. **Elimina**: su un backup automatico con due posti, la conferma ha 1 casella «anche la copia in sftp-a», non spuntata; confermata così, la riga resta con la sola pastiglia fuori sede; per P2 la casella non c'è.
11. **Riservatezza di P2**: DOM e JSON contengono 0 occorrenze di «lab3», «lab4», «sftp-a», «sftp://», «/var/», «/srv/», del nome del centrale; «sftp-ws» sì (è del suo workspace).
12. **Prima domenica**: con l'orologio di prova al primo sabato→domenica del mese, il giro della domenica di `acme-odoo` ha il passo «Prova dalla copia in sftp-a» e la pastiglia ha nel `title` «ripristino provato da questa copia»; le altre domeniche il passo non c'è.
13. **Attesa**: prime righe in < 2 s con 500 backup; le pastiglie fuori sede entro 5 s; con `sftp-ws` fermo, una riga «sftp-ws non risponde» entro 7 s e le altre righe ci sono.
14. **Telefono**: `scrollWidth` ≤ 375; pastiglie visibili in ogni blocco; menu, voci e select ≥ 44 px.
15. **Titoli e primari**: ogni `button`, `summary`, `a`, `select`, pastiglia e opzione disattivata ha un `title` non vuoto e diverso dal testo; `.primary:visible` = 0 sulla pagina Backup fuori dai dialoghi, 1 in ogni dialogo aperto; 0 `alert`, `confirm`, `prompt`.
16. **Registro**: dopo i passi 1-10, una riga per predefinita cambiata, destinazione di un'istanza cambiata, chiave mostrata, caricamento, scarico da una destinazione, eliminazione di una copia fuori sede.

## 19. Smell che fanno FAIL

- Un backup di una produzione rifiutato, o un aggiornamento fermo, perché manca la destinazione o la copia non riesce.
- Un'opzione «da nessuna parte» per una produzione; un campo destinazione su staging o test.
- Un backup a mano o di prima dell'aggiornamento che resta solo sulla macchina con una destinazione scelta.
- Una pagina, scheda o tabella a parte per le copie; lo stesso backup su due righe.
- Una pastiglia che dice il posto solo col colore; «✗» su un backup di prima della destinazione.
- La mancanza di destinazione mandata per e-mail ogni notte.
- Una macchina giù le cui copie non compaiono quando il centrale conosce le sue destinazioni.
- Elimina che cancella la copia fuori sede di un backup senza la casella; la casella per P2.
- Un nome di macchina, del centrale o di una destinazione del server nel DOM o nel JSON di P2.
- Screenshot presi solo come admin, o con il gruppo developer.

## 20. Decisioni

### Decise da Carlo (23/11/2026)

- **D1**: una destinazione per istanza e una predefinita per server, non un archivio unico: «va in base ad ogni istanza/server, perché ogni volta che faccio il backup deve andare da qualche parte».
- **D2**: niente copiato subito: dalla notte dopo (§3 regola 6).
- **D3**: fuori sede la stessa rotazione della macchina, e niente cancellazioni per una macchina che non risponde (la raccomandata, riletta: vale per ogni destinazione).
- **D4**: un backup caricato resta 30 giorni (§9).
- **D5**: una produzione nuova segue la predefinita del suo server (con D1 è la definizione stessa di predefinita).
- **D6**: la prima domenica del mese la prova di ripristino si fa dalla copia fuori sede.
- **D7** (la copia sul disco del centrale): non serve più, il centrale non tiene copie.

### Da decidere (la raccomandata per prima)

I numeri sono un'**ipotesi da misurare**: circa 12 produzioni, ultimo backup 2 × 6 GB, 4 × 1 GB, 6 × 50 MB = circa 16 GB a notte; il dato vero è nella testa della pagina Backup del centrale.

1. **D-N1 — Una produzione senza destinazione.**
   - **Raccomandata: il backup si fa, il job finisce «⚠ con avvisi», la produzione è da guardare nella campanella finché non ne ha una; nessuna opzione «da nessuna parte».** Esempio: lab4 senza predefinita con 3 produzioni: stanotte 3 backup sulla macchina, 3 job con l'avviso, 3 voci nella campanella, 0 e-mail; una scelta della predefinita le toglie tutte e 3 dal backup dopo.
   - Alternativa: **rifiutare** il backup. Stanotte 3 produzioni senza nessun backup, e un «Backup prima di un aggiornamento» rifiutato ferma l'aggiornamento.
   - Alternativa: avvisare solo nel job (com'è oggi). Nessuno apre 3 job a notte: la mancanza resta invisibile.
2. **D-N2 — Quali backup vanno fuori sede.**
   - **Raccomandata: tutti quelli di una produzione** (automatico, a mano, prima di un aggiornamento, archivio, caricato). Esempio `acme` da 6 GB: 30 automatici al mese (180 GB di traffico, come oggi con «Copia anche in») più circa 4 prima di un aggiornamento (24 GB) e 2 a mano (12 GB): +20% di traffico; fuori sede in più tiene gli ultimi 3 di prima dell'aggiornamento (18 GB).
   - Alternativa: solo gli automatici (oggi). Il backup fatto alle 15 prima di un aggiornamento rischioso è solo sulla macchina, ed è proprio quello che serve se la macchina cade alle 15:30.
3. **D-N3 — Staging e test.**
   - **Raccomandata: nessuna copia fuori sede e mai da guardare**: si rifanno dalla produzione. Esempio: 8 staging con un backup a mano di 1 GB ciascuna: 8 GB in meno fuori sede e 0 voci nella campanella.
   - Alternativa: la stessa destinazione della produzione: anche i dati delle prove sopravvivono alla macchina.
4. **D-N4 — Una predefinita anche per il workspace.**
   - **Raccomandata: no, due livelli come hai detto (server e istanza)**; P2 sceglie per le sue istanze. Esempio: un cliente con 2 produzioni e il suo Dropbox sceglie 2 volte un campo; con un terzo livello ogni istanza dovrebbe dire da quale dei tre eredita.
   - Alternativa: una predefinita del workspace fra quella del server e l'istanza: comoda per un cliente con 10 produzioni.
5. **D-N5 — Chi sa dove sono le copie di una macchina che si ferma.**
   - **Raccomandata: il centrale tiene, sigillata, una copia delle destinazioni (chiavi comprese) di ogni macchina collegata come admin**, aggiornata quando cambiano. Esempio: lab1 si ferma alle 03:00; alle 09:00 la pagina Backup del centrale mostra le sue 46 copie e in 3 clic una è un'istanza su lab2, senza digitare niente.
   - Alternativa: a mano: sul centrale o su lab2 si aggiunge la destinazione di lab1 con le sue impostazioni e la sua chiave di 44 caratteri, se qualcuno l'ha salvata; poi le copie compaiono. Al momento del guasto servono la password della destinazione e la chiave.
   - Questa è anche la rete della raccomandata, per quando il centrale non c'è più.

## Non verificato

- Quali destinazioni dicono lo spazio libero (`rclone about`): attese SFTP, Drive, Dropbox, OneDrive, WebDAV; non S3 né FTP.
- Il tempo di un elenco ricorsivo di una destinazione con 500 file (atteso 1-3 s): se supera 2 s, la cache di 10 minuti è la regola e la riga dice quando è stato letto.
- Se la prova a secco di `restore_new_instance` si può fare dai soli fatti salvati con la copia: oggi legge il dump.
- Quanto allunga un «Backup prima di un aggiornamento» la copia fuori sede, se l'AD la mette prima dell'aggiornamento (§12.2).
- Il contratto con i clienti: i loro backup in una destinazione dell'integratore, e le chiavi delle destinazioni del loro workspace tenute dal centrale (D-N5), sono trattamenti da controllare prima di toccare ber e ridix.
- Le misure (§20): ipotesi.

## Tracciabilità

Richiesta di Carlo del 23/11/2026 sul dialogo «Carica un backup» (5502), e le sue risposte del 23/11 alla prima versione (commit fff6bbf). Nessun `TS-NNN` né `T<n>`/`HT<n>`.
- UX-006 §2-3; UX-008 §2 regole 1-4, §3, §4.2; UX-009 §6 e la sorveglianza della mappa; UX-013 (P1 centrale, P2 diretto); AD-008…015 (AD-014: le destinazioni copiate dalla casa all'host, la base di D-N5).

Fonti nel codice:
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/backups.html`: lista, `#up-dialog`, `#fb-dialog`;
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/server.html`: «Copia anche in» (666-670), Backup automatici (650-690), «Copie in …» e «Riporta qui» (693-696, 2499-2530);
- `/home/carlo/Projects/OmniaCLI/omniacli/destinations.py`: `.ocpf`, `upload`, `rotate`, `listing`, `fetch`, `<path>/<server>/<role>/`;
- `/home/carlo/Projects/OmniaCLI/omniacli/schedules.py`: `DEFAULT_ROTATION` 7/4/12, `BackupStep.destination`, `verify_weekday`;
- `/home/carlo/Projects/OmniaCLI/omniacli/app.py`: `destination_rows` e `destination_save` (12949-12968), `IMPORT_KEEP_DAYS` e `imports_sweep` (14868-14948), `upload_ticket` (14314), `restore_new` (14492);
- `/home/carlo/Projects/OmniaCLI/omniacli/bell.py`: le voci da guardare;
- `/home/carlo/Projects/OmniaCLI/README.md`: «Backup destinations» (1943), «Instances and Backups of every machine» (1981), «Is a backup good» (3197), tabella delle azioni (Backup before an update, 251).
