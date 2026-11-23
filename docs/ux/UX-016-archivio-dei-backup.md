# UX-016 — I backup in un posto che sopravvive alla macchina, e «Carica un backup» che ci va dentro

Stato: contratto di design, prima della build. File proposto: `/home/carlo/Projects/OmniaCLI/docs/ux/UX-016-archivio-dei-backup.md`.
Lo implementa chi scrive `omniacli/templates/backups.html` (lista, dialogo «Carica un backup», dialogo «Ripristina in un'istanza nuova»), `destinations.html` (la scelta dell'archivio), la scheda Backup di `server.html` (la riga «Copia anche in» e la sezione «Copie in …») e le route di `omniacli/app.py` intorno a `/api/backups`, `imports/upload`, `instances/from-backup`. Jinja e JS senza framework, classi esistenti: `.panel`, `.badge`, `.small`, `.muted`, `.mono`, `.note-box`, `details.row-menu`, `dialog`, `.hide-sm`. Le stringhe vanno in `omniacli/i18n/it/*.json`.
Valgono le regole comuni e i criteri di UX-001…015, in particolare UX-006 §2-3 (al massimo 5 schede, date `gg/mm/aaaa hh:mm`, `title` su ogni pulsante), UX-008 §2 regole 1-4 (un primario che crea; una macchina che non risponde è una riga; un numero in testa è un filtro; una copia è neutralizzata), UX-008 §4.2 (la pagina Backup), UX-009 §6 (stati, segni e parole). Questo contratto li applica e non li ripete.

Origine: Carlo, 23/11/2026, sul dialogo «Carica un backup» (rilascio 5502): «i backup dovrebbero essere su una cartella unica, magari sul server locale, perché in questo modo si ha la protezione dei backup, che se dovesse andare giù il server i backup restano, oppure su quelle cartelle che si configurano dove si vogliono i backup, tipo FTP, Dropbox etc.»

**Prima della build serve un AD** (`software-architect`, AD-016): dove stanno le impostazioni e la chiave dell'archivio, chi sposta i byte di un ripristino su un'altra macchina, la ricevuta della copia alla macchina. Le domande sono in §12. Questo contratto decide lo schermo e lo dà per possibile.

---

## 0. Gate

UI utente non banale: **sì**. Cambia la lista d'uso quotidiano «i backup di stanotte ci sono, me ne serve uno» (UX-008 §1), il dialogo di caricamento e quello di ripristino, e aggiunge una scelta nelle Impostazioni. Rischio-accrescimento: aggiungere una seconda lista («Copie fuori sede») accanto a quella dei backup, o una colonna per ogni posto. Il contratto tiene **una riga per backup** e mette i posti dove sta dentro la riga.

## 1. Cosa c'è oggi (23/11/2026, `carlo_dev_2026`, a78420e, rilascio 5502)

| Fatto | Dove sta oggi | Difetto rispetto alla richiesta |
|---|---|---|
| Backup automatici | Scheda Backup dell'istanza › «Backup automatici»: ora, rotazione (default 7 giorni, 4 settimane, 12 mesi: `schedules.DEFAULT_ROTATION`), «Copia anche in» (una destinazione o «da nessuna parte: solo questa macchina»), archivio completo, «Ogni domenica prova a ripristinarlo», «Se fallisce, avvisa» | la copia fuori dalla macchina è **spenta di default** e si accende istanza per istanza: con 20 istanze, 20 dialoghi |
| Destinazioni | Impostazioni › Destinazioni dei backup (della macchina) e Workspace › Servizi esterni › Destinazioni (del workspace). rclone: SFTP, FTP, S3, Drive, Dropbox, OneDrive, WebDAV, Azure, GCS. Ogni copia cifrata con la chiave della destinazione (`.ocpf`, AES-256-GCM); stessa rotazione della macchina (`destinations.rotate`) | le impostazioni **e la chiave** stanno in `destinations.yaml` della macchina: se la macchina muore, nessuno sa più dove sono le copie né come leggerle, a meno che qualcuno abbia salvato la chiave («Mostra la chiave») |
| Copie in una destinazione | scheda Backup dell'istanza › `details` «Copie in <destinazione>», con «Riporta qui» | solo istanza per istanza, solo se la macchina risponde; la pagina Backup globale non le vede |
| Pagina Backup globale (`/backups`) | i backup di questa macchina e di ogni collegamento (`/api/backups`, `?link=`), 50 per pagina; colonne Istanza · Server · Fatto il · Dimensione · Esito · Azioni ▾ (Scarica, Ripristina in un'istanza nuova…, Elimina…) | una macchina che non risponde è una riga «lab1 non risponde: i suoi backup qui non ci sono». **È proprio il caso in cui servono** |
| «Carica un backup…» | dialogo: Server (questa macchina o un collegamento) → Workspace → File; il file va nell'area di import del workspace **su quella macchina**, 7 giorni (`IMPORT_KEEP_DAYS`), poi cancellato da `imports_sweep` | il file caricato sta sulla stessa macchina che deve proteggere; la scelta del Server viene prima di sapere dove servirà |
| Ripristina in un'istanza nuova… | staging della produzione, o istanza nuova di un workspace **della stessa macchina del backup** (il piano è la prova a secco di `restore_new_instance`) | da un backup di lab1 non si fa un'istanza su lab2 |
| Prova di ripristino | SHA-256 alla creazione; «Verifica» a mano; la domenica sul backup della macchina | nessuna prova che una copia fuori sede si decifri e si ripristini |
| Pannello centrale | l'OmniaCLI di Carlo (localhost:8090, sul suo PC), nessuna istanza né disco dei workspace; collegamenti **in uscita** verso lab1, lab2, ber, inaria, odoo-test, ridix | le macchine non raggiungono il centrale: non possono spingergli un file |

## 2. Persone e lavoro primario

| Persona | Chi è nel codice | Cosa fa qui |
|---|---|---|
| **P1 Integratore sul centrale** (Carlo) | `is_admin`, non `scoped`, `listed_links` non vuoto | sceglie l'archivio, controlla che ogni produzione abbia la copia fuori sede, ripristina su un'altra macchina quando una è giù |
| **P1b Admin della macchina, direttamente** (sul pannello di lab1) | `is_admin`, non `scoped` | come P1, per la sola sua macchina e i suoi collegamenti |
| **P2 Admin del workspace** (il cliente) | `scoped`, admin nel workspace | vede i backup delle sue istanze e se hanno la copia fuori sede; carica un backup; ripristina nel suo workspace. **Nessun nome di macchina, del centrale né il percorso della destinazione dell'integratore** (`customer_view`) |
| **P3 Sviluppatore** | ruolo `developer` | legge e scarica (UX-008 §3) |
| P4 Utente | ruolo `user` | la voce Backup non c'è (UX-008 §3) |
| P5 Ruolo personalizzato (5492) | capacità `backup`, `download`, `restore`, `instances` | quello che le capacità permettono; il resto disattivato con il motivo (`refused`) |

Device: desktop 1440×900. Telefono 375×812 per «stanotte è andata? la copia fuori sede c'è?» in sola lettura, più Scarica.

**Lavoro primario**: in 3 secondi, «di ogni produzione l'ultimo backup c'è, e c'è anche **fuori dalla macchina**?», e se una macchina è giù «i suoi backup dove sono, e li rimetto su un'altra».
Secondario: caricare un database da questo computer; ripristinarlo in un'istanza nuova su qualunque macchina.

## 3. Dove vivono i backup (D1)

**Un backup sta in due posti: sulla macchina che lo fa, e nell'«Archivio dei backup», un posto solo per tutte le macchine.**

- **Sulla macchina**, come oggi: è la copia veloce, quella della prova della domenica, del «Backup prima di un aggiornamento» e dei ripristini sul posto. Non cambia niente.
- **Nell'archivio**: una destinazione di oggi (SFTP, FTP, S3, Dropbox, Drive, WebDAV…), scelta **una volta** sul pannello centrale come «Archivio dei backup» e data alle macchine che P1 spunta. La macchina copia lì, cifrato, dopo ogni backup automatico (il «Copia anche in» di oggi, con l'archivio come valore predefinito). Il centrale ha le impostazioni e la chiave, quindi **legge l'archivio anche quando la macchina non risponde**: elenca le copie, le scarica, le ripristina altrove.
- **«Il server locale» di Carlo** ci sta dentro così: un NAS o un server in ufficio che espone SFTP è una destinazione come le altre. Se le macchine non lo raggiungono (dietro NAT), vale l'alternativa B di D1: il centrale tira le copie attraverso i collegamenti.
- La parola sullo schermo è **«Fuori sede»** (EN «Off-site»): dice perché la copia c'è. «Archivio dei backup» è il nome della scelta nelle Impostazioni, non una pastiglia.
- Una destinazione del workspace scelta dal cliente per una sua istanza (oggi possibile) **sostituisce** l'archivio per quell'istanza e conta come «fuori sede». Il centrale ne vede le copie solo attraverso la macchina, finché risponde (§12, domanda 5).

## 4. La pagina Backup, dall'alto

1. **Testa** (`h1` «Backup»), a destra il secondario **Carica un backup…** (come oggi: la pagina legge, nessun primario; UX-008 §2 r1).
2. **Riga dei conteggi** (`role="status"`), com'è oggi più un pezzo:
   «212 backup · 96 GB · stanotte: 12 riusciti, 0 non riusciti · **3 produzioni con l'ultimo backup solo sulla macchina**».
   - L'ultimo pezzo è un filtro (UX-008 §2 r3): mostra le 3 righe (l'ultimo backup di quelle produzioni), e il conteggio dell'elenco filtrato è 3. Con 0 il pezzo non c'è (少: un numero che non chiede niente non sta in testa).
   - La somma dei GB conta **ogni backup una volta**, non una volta per posto.
   - Archivio pieno oltre l'85% (dove la destinazione lo dice, §8): in fondo alla riga «· archivio pieno all'87% (870 di 1000 GB)» in `--warn`, link alle Impostazioni (P1); per P2 non c'è.
3. **Nessun archivio scelto** (P1, e solo finché non c'è): una `note-box` sopra la tabella, «I backup stanno solo sulle macchine che li fanno: se una si ferma, i suoi backup si fermano con lei. **Scegli l'archivio dei backup ›**» (link alle Impostazioni › Destinazioni dei backup). È l'unico invito; sparisce appena l'archivio c'è.
4. Ricerca (oltre 8 righe, come oggi), poi la tabella, poi il paginatore.

## 5. La riga: un backup, i suoi posti

**Una riga per backup**, non una per copia: il backup di `acme` del 23/11/2026 03:00 sulla macchina e la sua copia nell'archivio sono la stessa riga (stesso server, ruolo, nome del file e SHA-256).

Colonne: Istanza · Server (solo con più macchine, come oggi) · Fatto il · Dimensione · **Dove** · Esito · Azioni ▾.

**Dove** (nuova, al posto di nessuna: le altre restano come sono), pastiglie in ordine fisso, ognuna con parola e segno (mai solo colore):
- «Macchina ✓» — il file c'è sulla macchina. `title`: «Sulla macchina lab1, 1,2 GB, SHA-256 scritto il 23/11/2026 03:02» (P2: «Sulla macchina»).
- «Fuori sede ✓» — la copia c'è nell'archivio (o nella destinazione dell'istanza). Per P1 la pastiglia dice il nome della destinazione al posto di «Fuori sede» («wasabi ✓»), il `title` il tipo e il momento: «wasabi · S3 · copiata il 23/11/2026 03:41 · cifrata». Per P2 «Fuori sede ✓», `title` «Una copia cifrata fuori dalla macchina, copiata il 23/11/2026 03:41».
- Con la prova di ripristino della copia fuori sede (D6): il `title` aggiunge «ripristino provato da questa copia il 02/11/2026».
- **Pastiglia assente** = il backup non c'è in quel posto (ruotato via, mai copiato). Non si scrive «✗»: l'assenza è il dato, e il filtro della testa lo conta.
- **«Macchina ?»** in `--muted` quando la macchina non risponde: non si sa, non si dice «manca».
- Un backup in corso di copia: «Fuori sede …» con `title` «Copia in corso: job <titolo>», link al job.

Il resto della riga com'è oggi. L'**Esito** resta la prova di ripristino del backup («✓ ripristino provato 21/11/2026»): è la stessa per tutte le copie, perché lo SHA-256 è lo stesso.

**Un'esecuzione automatica la cui copia fuori sede non è riuscita** è una riga fallita come oggi (UX-008 §4.2), con il tipo «copia fuori sede» e il motivo, link al job: «Copia fuori sede non riuscita: l'archivio è pieno (998 di 1000 GB)». Il backup sulla macchina è la riga sotto, con «Macchina ✓» e senza «Fuori sede».

## 6. Una macchina che non risponde

È il caso per cui la richiesta esiste.
- La riga della macchina (oggi «lab1 non risponde: …. I suoi backup qui non ci sono.») diventa, se l'archivio ha copie sue: «**lab1 non risponde** (<motivo>): qui ci sono le sue 46 copie fuori sede. Si ripristinano su un'altra macchina.» in `--err` per la prima parte. Senza copie: il testo di oggi.
- Le sue copie sono righe normali, lette dal centrale nell'archivio, con «Macchina ?» e «Fuori sede ✓». Colonna Server: «lab1», con `title` «non risponde da 23/11/2026 02:10».
- **Rotazione sospesa**: le copie di una macchina che non risponde non si cancellano da sole (nessuna esecuzione automatica le ruota). Il `title` della riga della macchina lo dice: «Le sue copie fuori sede non ruotano finché non risponde: restano tutte.»
- Il menu della riga: **Scarica** (dal centrale, che la decifra mentre la manda), **Ripristina in un'istanza nuova…** (§7). Niente Elimina finché la macchina non risponde (prevenzione: una macchina giù non è il momento di cancellare la sua unica copia).
- Per P2 (sulla macchina giù non entra; se il suo workspace è su un'altra macchina che risponde, non è il caso): nessun messaggio sulla macchina. Il cliente il cui server è giù parla con l'integratore.

## 7. «Ripristina in un'istanza nuova…» da qualunque copia

Il dialogo di 5502 resta; cambia dove può andare.

1. **Macchina** (nuovo, primo campo; solo P1 e P1b, e solo con **2 o più macchine** che rispondono e hanno un template della versione di Odoo del backup): predefinita la macchina del backup se risponde, altrimenti la prima che va bene. Le macchine che non vanno bene sono opzioni disattivate con il motivo («ber · nessun template di Odoo 18», «ridix · non risponde»). P2 non la vede: il suo workspace è su una macchina sola.
2. **Cosa diventa**: come oggi, «Una staging di <produzione>» o «Un'istanza nuova».
   - La staging si fa solo sulla macchina della produzione: con la macchina del backup giù è disattivata con il motivo «La produzione è su lab1, che non risponde: fanne un'istanza nuova su un'altra macchina.».
3. **Il piano** (prova a secco) arriva **senza aspettare il trasferimento del file**: versione di Odoo, moduli installati e database sono letti una volta, quando il backup è copiato o caricato, e viaggiano con la copia (§11). La riga dello spazio dice anche il trasferimento: «Spazio: servono circa 7 GB, 41 liberi nel workspace. Il file arriva dall'archivio: circa 6 GB, una decina di minuti.»
4. **Crea l'istanza** avvia il job, che ha come primo passo «Porta il backup dall'archivio» (con avanzamento e ripresa), poi i passi di oggi. Annulla del dialogo: nessuna richiesta.

## 8. «Carica un backup…»: nell'archivio, non su una macchina

**Il file caricato va nell'archivio**, cifrato, e da lì diventa una staging o un'istanza su qualunque macchina (§7). La domanda «quale Server» sparisce dal dialogo: si decide al ripristino, quando serve.

Il dialogo, dall'alto:
1. Il testo (`#up-says`), riscritto: «Un backup .zip di Odoo (come lo fa il gestore dei database: dump.sql, filestore, manifest.json) o un dump SQL in chiaro di pg_dump (.sql), fino a 50 GB. **Va nell'archivio dei backup, cifrato, e ci resta 30 giorni (D4) anche se una macchina si ferma.** Dalla sua riga fai una staging o un'istanza nuova, sulla macchina che scegli allora.»
2. **Workspace** (a chi appartiene il file: chi lo vede, dove si potrà ripristinare). P1 sul centrale: «acme · lab1», «beta · lab2»…; P2: il suo, e il campo non c'è se ne ha uno solo.
3. **File**.
4. Avanzamento come oggi (parti da 16 MiB, ripresa), poi «Caricato: acme.zip · Odoo 18.0 · database acme · con allegati · 2,4 GB. Nell'archivio fino al 23/12/2026.» e il primario «Fai una staging o un'istanza…» (come oggi).
5. Riga nella lista: pastiglia «Caricato» nell'Istanza (come oggi), Dove: «Fuori sede ✓» solo. Colonna Server: «—» con `title` «Nell'archivio: si ripristina sulla macchina che scegli».

**Rete di sicurezza: nessun archivio.** Se l'archivio non c'è (o la macchina del workspace non lo ha), il dialogo fa quello di oggi: il file va nell'area di import del workspace sulla sua macchina, 7 giorni, e il testo lo dice: «Nessun archivio dei backup: il file resta sulla macchina di acme per 7 giorni.» Per P1 con un link «Scegli l'archivio ›». Il caricamento non si blocca mai per l'assenza dell'archivio.

**Spazio** prima di inviare: la dimensione del file contro lo spazio libero dell'archivio dove la destinazione lo dice (SFTP, Drive, Dropbox, OneDrive, WebDAV: sì; S3 e FTP: di solito no). Non leggibile: si invia e un rifiuto dell'archivio arriva come errore con il rimedio (§10).

## 9. La scelta dell'archivio (Impostazioni › Destinazioni dei backup, solo P1/P1b)

Nessuna pagina nuova: la pagina delle destinazioni di oggi ha, per ogni destinazione, un'azione in più.
- **«Usa come archivio dei backup…»** (`button small`; al massimo una destinazione è l'archivio, che porta la pastiglia «Archivio»). Apre un dialogo:
  1. **La chiave** (prevenzione): «Senza questa chiave le copie non si leggono da nessun'altra parte. Salvala fuori da qui (password manager, carta).» con la chiave e «Copia». La casella «Ho salvato la chiave fuori da qui» deve essere spuntata per proseguire. (Mostrare la chiave è già una riga del registro degli accessi.)
  2. **Le macchine che lo usano**: una casella per collegamento, **tutte spente** di default, ognuna con lo stato («lab1 · 2026.1.5502 ✓», «ridix · OmniaCLI più vecchio: aggiornalo per dargli l'archivio»). ber e ridix sono server di clienti: l'archivio ci arriva solo se P1 li spunta.
  3. **Le produzioni**: «Copia fuori sede accesa per le produzioni di queste macchine che non ne hanno una (14 di 20)»: la casella è accesa (D5); le produzioni che hanno già una destinazione la tengono.
  4. **Cosa copiare adesso** (D2): «L'ultimo backup di ogni produzione, adesso (12 file, circa 16 GB)» acceso.
  5. Primario del dialogo «Usa come archivio»; Annulla.
- La riga dell'archivio mostra poi lo spazio dove leggibile («870 di 1000 GB») e la data dell'ultima copia ricevuta.
- **Smettere di usarlo** («Non usarlo più come archivio…», `danger`): le macchine tornano a «solo questa macchina» per le produzioni che lo usavano; le copie già lì **restano** (il dialogo lo dice e dice quante).

Scheda Backup dell'istanza › Backup automatici › «Copia anche in»: il valore predefinito è l'archivio, scritto «Archivio dei backup (wasabi)»; «da nessuna parte: solo questa macchina» resta una scelta, con il `title` «Se la macchina si ferma, i backup si fermano con lei».

## 10. Il necessario

- **Stato del sistema**: le pastiglie «Macchina» e «Fuori sede» con parola e segno; «Fuori sede …» durante la copia, con il job; la riga di una macchina che non risponde con il numero delle sue copie; il conteggio «N produzioni con l'ultimo backup solo sulla macchina»; l'archivio pieno; le copie fallite come righe con il motivo.
- **Annulla**: ogni dialogo ha Annulla/Chiudi che non manda richieste; un caricamento si ferma e riprende (come oggi); «Non usarlo più come archivio» non cancella copie. Eliminare una copia fuori sede non si annulla: vedi sotto.
- **Prevenzione dell'errore**:
  - «Elimina…» su una riga cancella **il file della macchina**, come oggi. La copia fuori sede si cancella solo con la casella «anche la copia fuori sede (non si recupera)», spenta di default, e solo per P1/P1b. P2 non la vede (la copia fuori sede è la protezione anche dal suo errore; una richiesta GDPR passa dall'integratore).
  - Niente Elimina sulle righe di una macchina che non risponde.
  - L'archivio non si accende senza la casella «Ho salvato la chiave».
  - Nel ripristino, le macchine senza template o che non rispondono sono disattivate con il motivo; la staging disattivata quando la sua produzione è giù.
- **Messaggio d'errore** (tradotto, con il rimedio, senza nomi tecnici):
  - «L'archivio è pieno (998 di 1000 GB): libera spazio sulla destinazione o accorcia la rotazione delle produzioni più grandi.»
  - «La copia nell'archivio non si decifra con la sua chiave: la destinazione ha copie fatte con un'altra chiave. Dai alla destinazione la chiave di prima (Impostazioni › Destinazioni).»
  - «L'archivio non risponde (<motivo>): le copie fuori sede qui non ci sono. I backup sulle macchine sì.» (riga, come una macchina muta).
- **Aiuto**: `title` su ogni pastiglia, pulsante, voce di menu e opzione disattivata; il testo del dialogo di caricamento dice dove va il file e per quanto; la `note-box` «nessun archivio» dice perché serve.

## 11. Contratto dei dati (i nomi definitivi li decide chi implementa)

- `/api/backups` di ogni macchina aggiunge a ogni backup `places`: `{machine: {present, sha256_time}, offsite: {destination, label, copied, tried}}`. Per P2 `destination` e `label` non ci sono (`customerview`).
- Il centrale legge **l'archivio una volta** (un elenco ricorsivo della cartella, non uno per istanza), lo tiene 10 minuti, e lo unisce alle righe delle macchine per (server, ruolo, file). Le righe solo-archivio di una macchina che non risponde vengono da lì.
- **I fatti di una copia** (versione di Odoo, database, con/senza allegati, i moduli installati): letti quando il backup è copiato o caricato, salvati accanto alla copia **cifrati come lei**. Il piano del ripristino (§7.3) li usa senza scaricare il file.
- **La ricevuta**: dopo una copia riuscita la macchina scrive `copied` nel suo `.omniacli-backups.json`; è da lì che P2, che vede solo la sua macchina, legge «Fuori sede ✓».
- Il caricamento (§8) arriva al pannello che il browser usa (il centrale, o la macchina per P2), che lo cifra e lo manda all'archivio sotto la cartella del workspace; i caricati scadono dopo D4 giorni (un `imports_sweep` dell'archivio).
- Registro degli accessi (NIS 2): ogni caricamento, copia, scarico da archivio, eliminazione di una copia fuori sede, scelta e abbandono dell'archivio, chiave mostrata.

## 12. Domande per `software-architect` (AD-016, prima della build)

1. Le impostazioni e la chiave dell'archivio: sul centrale (l'originale) e copiate alle macchine spuntate, come AD-014 copia le destinazioni dalla casa all'host?
2. Il ripristino su un'altra macchina: la macchina che riceve legge l'archivio con rclone e la chiave, o il centrale le carica il file nell'area di import a parti (la route di 5502) e poi avvia `from-backup`?
3. Variante B di D1 (il disco del centrale): il centrale tira le copie attraverso i collegamenti (`linkcopy.fetch_backup`, senza ripresa oggi: AD-010 `treesync`?), con quali orari e quale recupero se il PC era spento.
4. La prova mensile della copia fuori sede (D6): dove si scarica e si ripristina, e con quale spazio.
5. Una produzione che copia in una destinazione **del workspace** (non l'archivio): il centrale ne vede le copie solo attraverso la macchina. Va bene così?

## 13. Visibilità

| Elemento | P1 centrale | P1b admin macchina | P2 admin workspace | P3 sviluppatore | P5 ruolo personalizzato | Meccanismo |
|---|---|---|---|---|---|---|
| Righe dei backup | ogni macchina + archivio | la sua + collegamenti | le sue istanze | come il suo ambito | le istanze a cui è limitato | `visible_servers`, `grant` |
| Colonna Dove | sì, con il nome della destinazione | sì | sì, «Macchina» / «Fuori sede» | sì | sì | `customer_view` |
| Righe solo-archivio di una macchina giù | sì | — (non ha il centrale) | — | se del centrale | — | centrale |
| Pezzo «N produzioni … solo sulla macchina» | sì | sì | sì, le sue | sì | con `backup` | filtro |
| `note-box` «nessun archivio» | sì | sì | — | — | — | `is_admin`, non `scoped` |
| «Archivio pieno all'87%» | sì | sì | — | — | — | `is_admin` |
| Carica un backup… | sì | sì | sì (nel suo workspace) | — | con `instances` | `upload_here`, `upload_links` |
| Campo Workspace del caricamento | sì, con la macchina | sì | solo con 2+ workspace | — | idem | — |
| Ripristina › campo Macchina | sì, con 2+ macchine adatte | idem | — | — | — | `is_admin`, non `scoped` |
| Scarica (anche dall'archivio) | sì | sì | sì | sì | con `download`, disattivato senza | `download_refused` |
| Elimina… (file della macchina) | sì | sì | sì (produzione: admin) | test | con `backup` | come oggi |
| Casella «anche la copia fuori sede» | sì | sì | — | — | — | `is_admin`, non `scoped` |
| «Usa come archivio dei backup…» | sì | sì (per la sua macchina) | — | — | — | `Depends(admin)` |
| Nome della macchina, del centrale, percorso della destinazione | sì | sì | **mai** (DOM e JSON) | come il suo `scoped` | idem | `customer_view` |

## 14. HIDE / DEFER

| Cosa | Dove va | Rete di sicurezza |
|---|---|---|
| Una lista o pagina «Copie fuori sede» separata | — : è la colonna Dove della stessa riga | — |
| Una colonna per ogni posto | una colonna Dove con pastiglie | — |
| `details` «Copie in <destinazione>» della scheda Backup dell'istanza | la lista della scheda prende la colonna Dove; una copia solo-fuori-sede è una riga con «Riporta qui» | finché la scheda non cambia, il `details` resta |
| Percorso della copia nell'archivio, SHA-256, cifratura | `title` della pastiglia | — |
| Spazio dell'archivio | Impostazioni › Destinazioni | in testa oltre l'85% |
| Campo Server del caricamento | sparisce: la macchina si sceglie al ripristino | senza archivio, il file va sulla macchina del workspace (come oggi) |
| Campo Macchina del ripristino | solo con 2+ macchine adatte e per P1/P1b | — |
| Pezzo «N produzioni … solo sulla macchina» | non c'è con 0 | — |
| `note-box` «nessun archivio» | sparisce quando l'archivio c'è | — |
| Elimina sulle righe di una macchina giù | non c'è | torna quando risponde |
| La chiave | dialogo «Usa come archivio…» e «Mostra la chiave» di oggi | — |

## 15. Vuoto, attesa, dati densi

- **Nessun backup**: il testo di oggi; con l'archivio vuoto in più: «L'archivio dei backup è pronto: la prima copia arriva con il prossimo backup automatico (stanotte alle 02:00).»
- **Archivio appena scelto, copie di prima**: i backup vecchi restano senza «Fuori sede» (D2): il filtro della testa conta solo l'ultimo di ogni produzione, quindi dopo la prima copia scende a 0.
- **Attesa**: le righe delle macchine arrivano come oggi; le pastiglie «Fuori sede» arrivano dall'elenco dell'archivio, con «…» al loro posto finché non c'è. **Le prime righe in < 2 s**; l'archivio entro 5 s o una riga «L'archivio non risponde: <motivo>» (UX-008 §2 r2), senza bloccare il resto.
- **Dati densi**: ~20 istanze × fino a 23 backup = ~460 righe: 50 per pagina come oggi; la colonna Dove ha al massimo 2 pastiglie corte.

## 16. Telefono (fino a 600 px, la soglia di oggi di `backups.html`)

- Ogni backup è il blocco di oggi: istanza in testa, il menu in alto a destra; sotto, in `.small`, data · dimensione; poi **le pastiglie Dove** sulla stessa riga dell'esito.
- Nessuno scorrimento orizzontale; il menu e le sue voci alti almeno 44 px.
- Il dialogo di caricamento c'è anche sul telefono (un cliente può avere il file lì); a tutta larghezza.

## 17. Pulsanti e link (`title` scritto dalla route, in inglese con l'italiano negli i18n)

| Elemento | Tipo | Etichetta | `title` |
|---|---|---|---|
| Carica un backup… | secondario | Carica un backup… | «Manda un backup di Odoo o un dump SQL da questo computer all'archivio dei backup, cifrato: resta 30 giorni. Dalla sua riga fai una staging o un'istanza su una macchina. Senza archivio va nell'area di import del workspace per 7 giorni.» |
| filtro della testa | link | N produzioni con l'ultimo backup solo sulla macchina | «Mostra l'ultimo backup delle produzioni che non ha una copia fuori sede: se la macchina si ferma, è perso» |
| pastiglia Macchina | `span[title]` | Macchina ✓ / Macchina ? | «Sulla macchina <m>…» / «<m> non risponde: non si sa se il file c'è ancora» |
| pastiglia Fuori sede | `span[title]` | Fuori sede ✓ / <destinazione> ✓ | «Una copia cifrata fuori dalla macchina, copiata il …» |
| Scarica | voce di menu | Scarica | oggi; da archivio: «Scarica la copia fuori sede, decifrata dal pannello mentre arriva. Non cambia niente.» |
| Ripristina in un'istanza nuova… | voce di menu | invariata | in più: «Da una copia fuori sede: su una macchina che scegli; il file arriva dall'archivio nel job.» |
| Elimina… | voce di menu `danger` | Elimina… | «Cancella il file da questa macchina. La copia fuori sede resta, a meno di spuntarla nella conferma. Non si annulla.» |
| Usa come archivio dei backup… | secondario `.small` | Usa come archivio dei backup… | «Fa di questa destinazione il posto unico dove le macchine spuntate copiano i backup delle loro produzioni, cifrati. Chiede di salvare la chiave.» |
| Non usarlo più come archivio… | `.small danger` | Non usarlo più come archivio… | «Le macchine smettono di copiare qui; le copie già fatte restano.» |

## 18. Criteri di accettazione (Playwright)

**Dove**: la build su **lab3/lab4** con il pannello locale avviato dal worktree; l'aspetto finale sul pannello centrale di Carlo con lab1/lab2 dopo il rilascio. **Mai su ber, ridix, inaria, odoo-test.**
**Dati**: una destinazione SFTP di prova (un container sftp sulla rete `omnia-lab`) come archivio; su lab3 una produzione con 5 backup (3 copiati, 2 di prima dell'archivio), su lab4 due produzioni (una senza «Copia anche in» prima dell'accensione); un backup caricato di 0,5 MB; un ruolo personalizzato senza `download`.
**Persone**: P1 (centrale locale), P1b (diretto su lab3), P2 (admin del workspace di lab3), P3, P5. Nessun gruppo developer nelle schermate del cliente. 1440×900 e 375×812, IT, tema chiaro e scuro.

1. **Una riga per backup**: con l'archivio acceso, `tr[data-file]` della lista = numero di backup distinti (macchina ∪ archivio), non la somma delle copie; un backup presente nei due posti ha 2 pastiglie in `[data-places]`.
2. **Pastiglie**: ogni riga non fallita ha ≥ 1 pastiglia in Dove; ogni pastiglia ha testo (non solo colore) e `title` non vuoto; per P1 la pastiglia fuori sede contiene il nome della destinazione.
3. **Filtro**: il pezzo della testa vale 1 (la produzione di lab4 senza copia); cliccato, le righe visibili sono 1 e `aria-pressed="true"`; dopo il backup automatico seguente, il pezzo non c'è (0 elementi).
4. **Macchina giù**: con `docker stop lab4`, entro 7 s la riga della macchina contiene «lab4 non risponde» e il numero delle sue copie; le righe di lab4 ci sono, con «Macchina ?» e «Fuori sede ✓»; nel loro menu 0 voci «Elimina…»; «Scarica» di una di esse dà un file il cui SHA-256 è quello scritto alla creazione.
5. **Ripristino altrove**: con lab4 fermo, «Ripristina in un'istanza nuova…» su un backup di lab4: il campo Macchina c'è, lab4 è `disabled` con il motivo, lab3 è scelta; «Una staging di …» è `disabled` con il motivo che contiene «non risponde»; il piano compare in < 3 s senza trasferimento (0 byte scritti nell'area di import di lab3 prima di «Crea»); dopo «Crea» il job ha come primo passo «Porta il backup dall'archivio» e finisce con l'istanza su lab3 in funzione.
6. **Caricamento**: il dialogo ha 0 `#up-machine` visibili; P1 ha il campo Workspace con le voci «<ws> · <macchina>»; dopo il caricamento la riga ha «Caricato» e solo «Fuori sede ✓», e l'archivio contiene un file `.ocpf` (non leggibile in chiaro: `unzip -l` fallisce); nessun file nuovo nell'area di import di lab3/lab4.
7. **Senza archivio**: tolto l'archivio, il dialogo dice «resta sulla macchina di … per 7 giorni» e il file finisce nell'area di import come oggi; per P1 c'è 1 `.note-box` con il link alle Impostazioni.
8. **Elimina**: su una riga con due posti, la conferma ha 1 casella «anche la copia fuori sede», non spuntata; confermata così, la riga resta con solo «Fuori sede ✓»; per P2 la casella non c'è (0 elementi).
9. **Scelta dell'archivio**: il pulsante di conferma è `disabled` finché «Ho salvato la chiave» non è spuntata; le caselle delle macchine sono tutte non spuntate all'apertura; Annulla → 0 richieste POST.
10. **Riservatezza di P2**: DOM e risposte JSON della pagina contengono 0 occorrenze di «lab3», «lab4», del nome della destinazione dell'integratore, di «sftp://», «/var/», «/srv/», del nome del pannello centrale.
11. **Attesa**: le prime righe in < 2 s con 460 backup; le pastiglie fuori sede entro 5 s; con l'archivio fermo, la riga «L'archivio non risponde» entro 7 s e le righe delle macchine ci sono.
12. **Telefono, 375 px**: `scrollWidth` ≤ 375; ogni blocco ha le pastiglie Dove visibili; menu e voci alti ≥ 44 px.
13. **Titoli**: ogni `button`, `summary`, `a`, pastiglia e opzione disattivata della pagina e dei dialoghi ha un `title` (o uno `span[title]`) non vuoto e diverso dal testo; 0 chiamate ad `alert`, `confirm`, `prompt`.
14. **Primario**: `.primary:visible` nella pagina = 0 (fuori dai dialoghi); in ogni dialogo aperto = 1.
15. **Registro**: dopo i passi 4-9, il registro degli accessi ha una riga per caricamento, scarico da archivio, eliminazione della copia fuori sede, scelta dell'archivio, chiave mostrata.

## 19. Smell che fanno FAIL (per `odoo-ux-tester`)

- Una pagina, una scheda o una seconda tabella per le copie fuori sede.
- Lo stesso backup su due righe (una per la macchina, una per l'archivio).
- Una pastiglia che dice il posto solo con il colore; «✗» o un allarme per un backup di prima dell'archivio.
- Una macchina giù le cui copie fuori sede non compaiono, o la riga «i suoi backup qui non ci sono» quando l'archivio ne ha.
- Il dialogo di caricamento che chiede ancora il Server.
- Un caricamento bloccato perché l'archivio non c'è.
- Elimina che cancella anche la copia fuori sede senza la casella spuntata, o la casella per P2.
- Un nome di macchina, del centrale o della destinazione dell'integratore nel DOM o nel JSON di P2.
- L'archivio acceso su una macchina che P1 non ha spuntato (ber, ridix).
- Gli screenshot presi solo come admin, o con il gruppo developer.

## 20. Decisioni di Carlo (la raccomandata per prima)

I numeri sono un'**ipotesi da misurare**: 20 istanze su lab1/lab2/ber/inaria/odoo-test/ridix, di cui circa 12 produzioni con backup automatici; ultimo backup di ogni produzione: 2 da 6 GB, 4 da 1 GB, 6 da 50 MB = **circa 16 GB a notte**. Rotazione di oggi 7 giorni + 4 settimane + 12 mesi = fino a 23 backup per produzione: **circa 200 GB dopo un mese, 370 GB dopo un anno**. Il numero vero è nella testa della pagina Backup del centrale («N backup · X GB»). I prezzi sono da verificare.

1. **D1 — Dove sta la copia che sopravvive.**
   - **Raccomandata: una destinazione unica, l'«Archivio dei backup»**, scelta sul centrale e data alle macchine che spunti. Le macchine copiano lì da sole, ognuna con la sua banda, anche col PC spento. Esempio: una Storage Box SFTP da 1 TB (circa 4 €/mese) tiene i 370 GB di un anno; se lab1 si ferma, le sue 46 copie sono nella pagina Backup e si ripristinano su lab2. Un NAS in ufficio raggiungibile in SFTP è la stessa cosa, «il server locale».
   - Alternativa: il **disco del pannello centrale**. Le macchine non raggiungono il tuo PC, quindi è il centrale a tirare le copie: 16 GB a notte a 12 MB/s (100 Mbit/s) sono circa 23 minuti, **solo se il PC è acceso**; 370 GB sul disco del PC; se si guasta il PC, le copie vanno con lui; i dati dei clienti stanno sul tuo PC.
   - Alternativa: **tutte e due**: l'archivio per tutto, e sul centrale l'ultimo di ogni produzione (16 GB) per ripristinare in fretta.
2. **D2 — Cosa si copia quando accendi l'archivio.**
   - **Raccomandata: l'ultimo backup di ogni produzione, subito** (12 file, 16 GB, circa 15 minuti per macchina a 20 MB/s); i più vecchi restano solo sulla macchina ed escono con la rotazione. Dopo 7 giorni l'archivio ha le 7 notti; dopo un anno tutta la rotazione.
   - Alternativa: tutta la storia (circa 200 GB oggi, ore di banda per macchina, anche di notte di ber e ridix).
   - Alternativa: niente subito, si parte dal backup automatico della notte dopo (per un giorno nessuna protezione).
3. **D3 — Quanto tiene l'archivio.**
   - **Raccomandata: la stessa rotazione della macchina** (com'è oggi per le destinazioni), e **niente cancellazioni per una macchina che non risponde**. Esempio: lab1 giù per 10 giorni: le sue 23 copie restano tutte, anche se in quei giorni ne sarebbero uscite 7.
   - Alternativa: più corta, le ultime 7 notti (circa 112 GB): meno spazio, ma un errore scoperto dopo 8 giorni non ha più la copia fuori sede.
   - Alternativa: più lunga della macchina (per esempio 24 mesi): circa 560 GB.
4. **D4 — Quanto resta un backup caricato.**
   - **Raccomandata: 30 giorni nell'archivio**, poi cancellato da solo (Elimina… prima). Esempio: la migrazione di un cliente con un dump da 6 GB: 30 giorni costano 6 GB per un mese, e coprono le prove di una migrazione.
   - Alternativa: 7 giorni, come oggi.
   - Alternativa: finché non lo elimini a mano.
5. **D5 — Una produzione nuova, su una macchina con l'archivio.**
   - **Raccomandata: «Copia anche in» acceso sull'archivio di default**; staging e test no (sono copie). Esempio: dopo l'accensione, 14 produzioni su 20 senza copia fuori sede passano a 0 senza aprire 14 dialoghi.
   - Alternativa: spento, si accende a mano istanza per istanza (oggi: 14 dialoghi).
6. **D6 — Provare la copia fuori sede.**
   - **Raccomandata: la prima domenica del mese la prova di ripristino si fa dalla copia fuori sede** (scaricata, decifrata, ripristinata in un database a parte, buttata), le altre domeniche da quella della macchina. Prova che chiave e copia funzionano. Costo: 16 GB scaricati al mese, un'ora di prove.
   - Alternativa: mai, si prova solo il backup della macchina (oggi). Una chiave sbagliata si scopre il giorno in cui serve.
7. **D7 — La copia sul disco del centrale (solo con D1 B o C).**
   - **Raccomandata: cifrata come le destinazioni**, con la stessa chiave dell'archivio: un PC si perde, e i backup sono dati dei clienti.
   - Alternativa: in chiaro, affidandosi alla cifratura del disco del PC.

## Non verificato

- Quali destinazioni dicono lo spazio libero (`rclone about`): atteso sì per SFTP, Drive, Dropbox, OneDrive, WebDAV, no per S3 e FTP; da provare.
- Il tempo di un elenco ricorsivo dell'archivio con 460 file su S3 e su SFTP (atteso 1-3 s): se supera 2 s, la cache di 10 minuti diventa la regola e la riga dice quando è stato letto.
- Se la prova a secco di `restore_new_instance` si può fare dai soli fatti (moduli, versione) senza il dump: oggi legge il file (§11, §12 domanda 2).
- Le misure dei backup e il numero delle produzioni (§20): ipotesi, da leggere sul centrale.
- Il contratto con i clienti: i loro backup in una destinazione dell'integratore (o sul suo PC) è un trattamento di dati personali; da controllare prima di spuntare ber e ridix.

## Tracciabilità

Richiesta di Carlo del 23/11/2026, in chat, sul dialogo «Carica un backup» del rilascio 5502. Non ci sono un `TS-NNN` né un `T<n>`/`HT<n>`.
- UX-006 §2-3; UX-008 §2 regole 1-4, §3, §4.2 (pagina Backup); UX-009 §6; UX-013 (P1 centrale, P2 diretto); UX-014 e AD-008…015 (AD-014: destinazioni copiate dalla casa all'host; AD-010: `treesync`).

Fonti nel codice:
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/backups.html`: la lista (`backupRow`, `outcomeOf`, `machineRow`), il dialogo di caricamento (`#up-dialog`, `upTargets`, `askPlaces`), il ripristino (`#fb-dialog`, `planUrl`);
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/server.html`: Backup automatici e «Copia anche in» (650-690), «Copie in …» con «Riporta qui» (693-696, 2499-2530);
- `/home/carlo/Projects/OmniaCLI/omniacli/destinations.py`: cifratura `.ocpf`, `upload`, `rotate`, `listing`, `fetch`, `<path>/<server>/<role>/`;
- `/home/carlo/Projects/OmniaCLI/omniacli/schedules.py`: `DEFAULT_ROTATION` 7/4/12, `BackupStep.destination` (una sola), `verify_weekday`;
- `/home/carlo/Projects/OmniaCLI/omniacli/app.py`: `IMPORT_KEEP_DAYS` = 7 e `imports_sweep` (14868-14948), `upload_ticket` (14314), `restore_new` (14492);
- `/home/carlo/Projects/OmniaCLI/omniacli/mcp_server.py`: `upload_backup`, `restore_new_instance` (972-1014);
- `/home/carlo/Projects/OmniaCLI/README.md`: «Backup destinations» (1943), «Instances and Backups of every machine» (1981), «Is a backup good» (3197), «Pullers» (869);
- `/home/carlo/Projects/OmniaCLI/docs/adr/AD-008-consegna-e-spostamento.md`: AD-010, AD-014 (cosa sta sulla casa e cosa sull'host).

## Risposte di Carlo (23/11/2026)

- D1: **non un archivio unico**: «va in base ad ogni istanza/server, perché ogni volta che faccio il backup deve andare da qualche parte». Ogni istanza (e ogni server, come default delle sue istanze) ha la sua destinazione, e ogni backup ci va.
- D2: niente subito, dalla notte dopo.
- D4: un backup caricato resta 30 giorni.
- D6: la prima domenica del mese la prova di ripristino si fa dalla copia fuori sede.
- D3, D5, D7: la raccomandata, da rileggere alla luce di D1.
