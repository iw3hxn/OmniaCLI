# UX-015 — Le build degli ambienti, come in Odoo.sh

Stato: contratto di design, prima della build. File proposto: `/home/carlo/Projects/OmniaCLI/docs/ux/UX-015-build-degli-ambienti.md`.
Lo implementa chi scrive `omniacli/templates/server.html` (scheda Panoramica, tabella Ambienti), `_deploys.html` (il dialogo «Torna a questo deploy…», che diventa condiviso) e le route di `omniacli/app.py` intorno a `deploys_list`. Jinja e JS senza framework, classi esistenti: `.panel`, `.badge`, `.state-dot`, `.small`, `.mono`, `.muted`, `.note-box`, `details.more-menu`, `dialog.sd`, `.hide-sm`. Le stringhe vanno in `omniacli/i18n/it/*.json`.
Valgono le regole comuni e i criteri di UX-001…014, in particolare UX-006 §2-3 (al massimo 5 schede, un primario per scheda, date `gg/mm/aaaa hh:mm`, `title` su ogni pulsante), UX-008 §2 regole 1-3, UX-009 §6 (stati, segni e parole) e UX-013 §B (Entra come… sulle righe). Questo contratto li applica e non li ripete.

Origine: Carlo, 23/11/2026: «una cosa di usabilità che mi piace è come in odoo.sh la gestione delle build», con lo screenshot della pagina Builds di un progetto Odoo.sh.

---

## 0. Gate

UI utente non banale: **sì**. È la vista d'uso quotidiano dell'integratore e dello sviluppatore («cosa gira dove, con che codice, e com'è andato l'ultimo aggiornamento»). Rischio-parità: replicare Odoo.sh (filtro, tre sezioni, card per ogni build) dove OmniaCLI ha meno righe e dati diversi. Il contratto riprende la **funzione** di Odoo.sh e la esprime con la tabella Ambienti che esiste già.

## 1. Cosa c'è oggi (23/11/2026, `carlo_dev_2026`, a812abd)

| Fatto | Dove sta oggi | Difetto rispetto alla richiesta |
|---|---|---|
| Ambienti di una produzione | Panoramica › tabella «Ambienti» (`server.html` 403-434): Indirizzo · Tipo · Copiata il · Stato · azioni (`Apri ↗`, `Entra come…`, `⚙ Indirizzi…`). Righe: produzione, ↳ test, ↳ ogni staging, ↳ staging su altre macchine (JS, `/stagings/elsewhere`) | non dice **con che codice** gira ogni ambiente né **com'è andato** l'ultimo aggiornamento |
| Storico dei deploy | Moduli › «Deploy» (`_deploys.html`): tabella Quando · Codice · Avviato da · Esito · Backup prima · «Torna a questo deploy…»; ultimi 100 (`deploys.KEEP`) | solo l'ambiente in vista: per confrontare produzione, test e staging servono 3 pagine e 3 clic sulla scheda Moduli |
| Commit arrivato su una produzione | `note-box` sopra le schede, visibile in **ogni** scheda (`server.html` 327-331), e di nuovo in Moduli › Deploy (`#dp-available`) | due posti, nessuno accanto alla riga della produzione |
| CI | Moduli › Aggiornamenti del codice: l'interruttore (test e staging); produzione spenta e disattivata | lo stato non si vede dalla riga dell'ambiente |
| Ricostruisci… | solo nella `note-box` della pagina della staging | dalla produzione non si arriva alla Ricostruisci di una sua staging senza aprirla |
| Schede della pagina | Panoramica · [Posta] · Backup · Log · Monitoraggio · Moduli · Impostazioni = **6** sulla produzione, **7** su una staging | già oltre il tetto di 5 di UX-006 §2: un'altra scheda farebbe 7 e 8 |

Cosa manca nei dati (lo aggiunge chi implementa, §11):
- **l'autore del commit**: `deploys.changes()` registra il messaggio (`%s`), non l'autore. `by` è chi ha *avviato* il job (una persona, «push (18.0)», l'aggiornamento automatico), non chi ha scritto il commit;
- **la versione di Odoo per deploy**: non è registrata. È la stessa per tutti gli ambienti di una produzione (stesso template) e sta già nella riga dei fatti della testata;
- **un job del codice fallito che non ha spostato nessun commit** (un Git pull con conflitto): oggi non è un deploy, quindi non c'è (D4);
- **la scadenza delle staging**: non esiste. `stagings.py` registra solo `parent`, `created`, `backup`; il README non ha nessuna regola di pulizia (D5).

## 2. Persone e lavoro primario

| Persona | Chi è nel codice | Cosa fa qui |
|---|---|---|
| **P1 Integratore**, admin della macchina | `is_admin`, `machine_admin` | guarda le tre righe, porta un commit in produzione, ricostruisce una staging, entra come un utente |
| **P2 Admin del workspace** | `is_admin_here`, `scoped` | come P1, senza Shell e senza nomi di macchine (`customer_view`) |
| **P3 Sviluppatore** (del workspace o della macchina) | ruolo `developer` | «il mio push è arrivato sul test? è andato bene?», torna indietro sul test |
| P4 Utente del workspace | ruolo `user`, `can_act` falso | vede gli ambienti e li apre, non lo storico del codice |
| P5 Ruolo personalizzato (5492) | `refusal(me, <capacità>)` | quello che le sue capacità `code`, `run`, `logs`, `login_as`, `backup`, `stagings`, `names` permettono; il resto disattivato con il motivo |

Device: desktop 1440×900. Telefono 375×812 per «è andato il deploy?» in sola lettura, più Apri.

**Lavoro primario**: in 3 secondi, per ogni ambiente della produzione, «gira? con quale commit? l'ultimo aggiornamento è andato bene?», e da lì entrarci.
Secondario: portare il commit nuovo in produzione, tornare a un deploy, ricostruire una staging.

## 3. Dove vive (D1)

**Raccomandato: la tabella «Ambienti» della scheda Panoramica diventa la vista delle build.** Nessuna scheda nuova.
- In Odoo.sh la pagina Builds **è** la pagina del progetto, e un progetto Odoo.sh è una produzione con le sue staging e i suoi branch di sviluppo: da noi è la pagina dell'istanza, e quella pagina si apre già su Panoramica › Ambienti.
- Le righe ci sono già (produzione, test, staging, staging altrove); si aggiunge a destra di ognuna la striscia delle sue card. La domanda della scheda (UX-006 §1: «Gira? Le sue copie dove sono?») diventa «Gira, con che codice, e le sue copie?»: stessa persona, stesso momento.
- Titolo della sezione: «Ambienti e deploy» (EN «Environments and deploys»). La parola «build» non entra: in OmniaCLI non si costruisce un'immagine per commit, si porta il codice; «deploy» è già la parola di Moduli › Deploy e del README.
- **Pagina del test** (`?view=test`) **e pagina di una staging**: la stessa sezione, con la sola riga dell'ambiente in vista. La produzione resta nella riga dei fatti («Istanza di produzione: acme ›»).
- **Lista Istanze e lista Workspace: niente card.** Sono liste di molte istanze: una striscia per riga sarebbe una griglia illeggibile. Un deploy fallito arriva già alla campanella e a «N da guardare» (job fallito, `bell.py`), e quella resta la rete.
- **Moduli › Deploy resta**, come storico intero (100 righe, con il backup di prima e i secondi senza servizio). La striscia è il riassunto, la tabella è l'archivio (D3). «Altri N ›» porta lì.

## 4. La sezione, dall'alto

1. **Testa**: `h2` «Ambienti e deploy»; a destra il primario di oggi, **+ Nuova staging** (UX-006 r1: è l'azione che crea della scheda). Nessun altro primario.
2. **Filtro «Filtra gli ambienti…»**: solo con **più di 8 righe** (`_list.html`, come UX-013 §A4.3). Con 1 produzione, 1 test e 0-3 staging non c'è: in Odoo.sh serve per decine di branch di sviluppo, qui le righe sono al massimo 5 nel caso tipico.
3. **Nessuna intestazione di sezione** Produzione / Staging / Sviluppo: con 3-5 righe tre titoli sono cornice. Il tipo lo dice la pastiglia di ogni riga (già oggi: «Produzione», «Test», «Staging»), e l'ordine è fisso: **produzione, test, staging di questa macchina dalla più nuova, staging delle altre macchine**.
4. **Una riga per ambiente** (§5), alta quanto la sua card più alta (circa 128 px).
5. In fondo, come oggi: «Sulla mappa ›» e l'empty-state delle staging.

## 5. La riga: la colonna a sinistra (circa 280 px)

Dall'alto:
1. **Stato**: `.state-dot` con la parola (UX-009 §6: «In funzione», «Ferma», «Occupata»). Una staging ferma è `--muted`, non un allarme.
2. **Nome** (link alla sua pagina; per la produzione è testo, è la pagina in cui si è), con la pastiglia del tipo. Per una staging altrove: «su lab4» in `.small` (per P2 «su un'altra macchina»).
3. **Branch**, in `.small mono`: «18.0». Se i repository dell'ambiente seguono branch diversi: «3 repository», con il `title` che li elenca («web: 18.0 · Common: 18.0-acme · …»).
4. **Riga dei segni**, in `.small`, separati da « · »:
   - **CI**: «CI accesa» (`--ok`) / «CI spenta» (`--muted`) per test e staging; «a mano» per la produzione, con il `title` «Una produzione si aggiorna solo a mano: Aggiorna porta il codice nuovo. La CI gira sul test e sulle staging.». È un link a Moduli › Aggiornamenti del codice di quell'ambiente, **non** un interruttore (accenderla genera un segreto e cambia il codice degli altri: resta dov'è, con il suo ruolo);
   - **Repository ↗**: link alla pagina del branch del primo repository (`workspaceops.web_url` + `/tree/<branch>`), solo per GitHub e GitLab. Con più repository, il `title` dice «Apre <nome>; gli altri 2 sono in Moduli › Codice»;
   - per una staging: «copia del 21/11/2026» (la colonna «Copiata il» di oggi, ripiegata qui).
5. **Ricostruisci…** (solo staging, `button small`, secondario): apre il dialogo di oggi (Solo i dati / Dati e codice). Per una staging altrove: disattivato, con il motivo di oggi («La sua produzione è su lab4: …»). Per chi non ha la capacità `stagings`, o `rebuild` negato: disattivato con il motivo di `refused`.

Tolto dalla riga rispetto a oggi: la colonna «Indirizzo» (è il nome), «Copiata il» (va nei segni), e la cella delle azioni (`Apri ↗ · Entra come… · ⚙ Indirizzi…`), che passa nella card di adesso (§7).

## 6. Le card: la striscia a destra

**Una card per deploy, la più nuova a sinistra.** Larghezza circa 240 px.

**Quante**: da 1280 px in su, **3 card** (quella di adesso e le 2 di prima); fra 900 e 1280, **2**; sotto 700, **1** (§10). Poi, a destra dell'ultima, il link **«Altri N ›»** a Moduli › Deploy di quell'ambiente (`?view=…#deploys`, o la pagina della staging). Le card con priorità **non spariscono mai dietro «Altri»**: la card «in corso», la card «disponibile» e la card di adesso stanno sempre fra quelle visibili, anche se la striscia supera il numero (rete di sicurezza).

**Cosa dice una card**, in quest'ordine:
1. **Il messaggio del commit**, in grassetto, una riga, troncato con «…» (intero nel `title`). È un link ↗ alla pagina del commit (`<web>/commit/<sha>`) quando il repository è su GitHub o GitLab; il link sostituisce il pulsante «GitHub» della card di Odoo.sh (少: un bersaglio solo per la stessa destinazione).
   - **Più repository nello stesso deploy**: il messaggio è quello del repository che ha portato più commit (a parità, il primo nell'ordine dei repository dell'istanza). Sotto, in `.small muted`: «+ 2 repository», con il `title` «web: 3 commit · Common: 1 commit».
   - Un repository aggiunto: «aggiunto <nome>»; tolto: «tolto <nome>» (i testi di oggi di `_deploys.html`).
2. **L'autore del commit** (§11, nuovo dato). Se il deploy è vecchio e non lo ha: chi l'ha avviato (`by`, già tradotto da `customerview` per P2). Il `title` della card dice sempre entrambi: «Commit di Mario Rossi · portato da push (18.0)».
3. **Età e durata**, in `.small`: «2 giorni fa · 0:33». L'età è relativa (D6), con il momento esatto (`whenText`) nel `title`; la durata è `finished − started` in `m:ss` (oltre l'ora `h:mm:ss`). Un deploy senza servizio misurato (`unserved`) aggiunge « · 0 s senza servizio» solo se è più di 0.
4. **L'esito**, pastiglia con parola e segno (mai solo colore):
   - «✓ riuscito» `badge ok`;
   - «⚠ con avvisi» `badge warn`;
   - «✕ fallito» `badge err`, e la card ha il bordo `--err`;
   - «fermato» / «interrotto» `badge` neutro.
   La versione di Odoo **non** va sulla pastiglia: sarebbe la stessa su ogni card (dato duplicato). Sta nei fatti della testata.
5. **Una sola azione per card** (§7-8).

**Card speciali** (sempre a sinistra delle altre, in quest'ordine):
- **In corso**: un job lavora sull'ambiente (`busy`). «⟳ in corso: <titolo del job>», link al job, nessuna azione. Si toglie quando il job finisce e la striscia si rilegge.
- **Disponibile, non portato** (solo produzione, da `available_now`): bordo tratteggiato, «Disponibile, non portato» al posto dell'esito, il messaggio e il commit come sopra, «arrivato con il push di 2 ore fa». Azione: **Aggiorna…** (`button small`, secondario), che apre il dialogo di oggi della testata. Questa card **sostituisce la `note-box` sopra le schede** (§9).
- **Adesso senza deploy**: se nessun deploy registrato corrisponde al codice di adesso (istanza nuova, codice cambiato fuori da OmniaCLI), la prima card è «Il codice di adesso», con il messaggio del commit di ogni repository letto dal `.git` (nessun fetch), «non portato da un deploy registrato» in `.small muted`, e l'azione di adesso (§7).

**La card di adesso** (`current`) ha la pastiglia «adesso» accanto all'esito e il bordo `--ok`; le altre no. Una sola card «adesso» per riga.

## 7. La card di adesso: «Apri ▾»

Un pulsante diviso, come «Connect ▾» di Odoo.sh: la parte principale **Apri ↗** apre Odoo in una scheda nuova (`instance_link`, come «Apri ↗» di oggi); la freccia apre un `details.more-menu` (il componente di «Altro ▾», non uno nuovo). Voci, in ordine di frequenza:

| Voce | Cosa fa | Chi la vede | Meccanismo |
|---|---|---|---|
| Entra come… | il dialogo di oggi, sull'ambiente della riga (`_login_as_dialog.html`) | le regole di UX-013 §B3; disattivata con il motivo per il ruolo personalizzato senza `login_as`, per un ambiente fermo | `row_login_as` |
| Log | apre la scheda Log di quell'ambiente | capacità `logs` | `denied.logs` |
| Backup adesso… | il dialogo di oggi (`data-backup`), per quell'ambiente | capacità `backup` | `can_act`, `denied.backup` |
| Shell | apre in una scheda nuova la shell nel container di Odoo (`/terminal`), altrimenti quella dell'utente di OmniaCLI (`/shell`) | solo `can_act and not scoped`, come oggi in «Altro ▾» | `show_terminal`, `own_user_shells` |
| ⚙ Indirizzi… | il dialogo di oggi | capacità `names` | `addresses_button` |

- «Apri ↗» non c'è se l'ambiente non ha un indirizzo che si apre: resta la freccia sola, con «Azioni ▾».
- Un ambiente fermo: «Apri ↗» disattivato con il motivo «<nome> è ferma: avviala dalla sua pagina.»; il menu resta.
- Una voce che il ruolo non ha **non c'è**; una voce che il ruolo personalizzato toglie è **disattivata con il motivo** (come i pulsanti di oggi, `refused`). Un menu con 0 voci non mostra la freccia.
- Questo **sostituisce la cella delle azioni della riga** (UX-013 §B2 per la tabella Ambienti): «Entra come…» passa da visibile a una voce del menu, un clic in più. La lista Istanze e l'espansione della lista Workspace tengono il loro «Apri ↗ · Entra come…» visibile.
- Riavvia, Ferma, Aggiorna, Ricostruisci non entrano nel menu: cambiano l'ambiente e stanno nella testa della sua pagina (e Ricostruisci nella colonna della staging).

## 8. Le card di prima

- **Riuscita o con avvisi**: l'azione è **Torna qui…** (`button small`, secondario; `title`: quello di oggi di «Torna a questo deploy…»). Apre il dialogo di oggi (`dp-dialog`), che diventa condiviso fra la Panoramica e Moduli › Deploy (un dialogo per pagina).
  - **Test e staging**: chi ha la capacità `code` (sviluppatore in su), come oggi.
  - **Produzione**: anche il nome dell'istanza scritto per confermare (D2).
  - «Torna qui…» non c'è su una card «disponibile», su quella di adesso, né senza repository (`can_back` falso).
- **Fallita**: bordo e pastiglia `--err`; sotto la pastiglia, in `.small`, **la prima riga dell'errore** (`error`, già tradotto e già senza nomi dell'integratore per P2), troncata a 2 righe con il testo intero nel `title`. L'azione è **«Apri il job ›»** (link al job, o «job non più nell'elenco» in `.muted` se `job` è nullo). Nessun «Torna qui…» su un deploy fallito: il codice di un deploy fallito può essere a metà.
- **Fermata, interrotta**: come la fallita, senza bordo rosso.

## 9. Cosa va via, cosa si sposta

| Oggi | Dopo | Perché |
|---|---|---|
| `note-box` «Nuovo commit disponibile…» sopra le schede (in ogni scheda) | la card «Disponibile, non portato» della riga della produzione; resta `#dp-available` in Moduli › Deploy | stesso fatto, accanto all'ambiente a cui manca; le schede Backup, Log, Monitoraggio, Impostazioni non servono a portare codice |
| Colonne Indirizzo, Copiata il, cella azioni della tabella | colonna a sinistra (§5) e card di adesso (§7) | una riga per ambiente, letta da sinistra a destra |
| Moduli › Deploy | **resta** (D3), con «Torna a questo deploy…» | storico intero; la striscia è il riassunto |
| Ricostruisci… nella `note-box` della pagina della staging | resta lì **e** sulla riga della staging nella produzione | sono due pagine; sulla stessa pagina compare una volta sola |

## 10. Telefono (fino a 700 px)

- Ogni riga diventa un blocco: in testa stato, nome e pastiglia del tipo; sotto, in `.small`, branch e segni; poi **una card sola**, la più importante (in corso › disponibile › adesso), a tutta larghezza, con «Apri ▾».
- Sotto la card: «Altri N deploy ›» verso Moduli › Deploy.
- Ricostruisci… resta sulla riga della staging (alta 44 px).
- Niente scorrimento orizzontale. Bersagli tattili alti almeno 44 px, anche le voci del menu.
- È una deroga a UX-013 §B5 (0 pulsanti nelle righe sul telefono): lì due pulsanti accanto a nome e stato non stavano in 375 px; qui la card è un blocco a sé, a tutta larghezza, e il pulsante è uno.

## 11. Contratto dei dati (i nomi definitivi li decide chi implementa)

- **Una richiesta per la sezione**, non una per card: per ogni ambiente della produzione (produzione, test, staging di questa macchina) le card visibili, il conteggio totale e `ci`, `can_back`, `available`. Le staging altrove una richiesta per collegamento, in parallelo, attraverso `/links/<m>/…`, con il timeout di 5 s di `LIST_TIMEOUT`; una macchina muta dà alla sua riga «Deploy non letti: <motivo di `describe_error`>», una più vecchia «lab4 ha un OmniaCLI più vecchio: aggiornalo per vedere qui i suoi deploy».
- **Nuovo campo per repository di un deploy: `author`** (`git log -1 --format=%an <after>`, come oggi `subject`). Nessuna migrazione: i deploy di prima ne sono senza (§6.2). Per P2 l'autore è un nome del repository del cliente: passa com'è.
- **Il codice di adesso quando nessun deploy è `current`**: per repository `{sha, subject, author}` da `deploys.snapshot` più `git log -1` sul commit. Nessun `git fetch` mentre la pagina aspetta (i «commit da portare» restano nella riga dei fatti e in Moduli › Codice).
- **Durata**: `finished − started` del deploy (ci sono già).
- **In corso**: dal `busy_job` di oggi, per ambiente.
- **Ruoli**: la striscia arriva solo a chi ha la capacità `code` (la route di oggi `deploys_list` è `Depends(developer)` più `code` in `capabilities.ROUTES`); per gli altri la riga arriva senza card (§12). Il filtro sta nel server.
- Le voci del menu della card di adesso arrivano già decise dal server per ogni ambiente (visibile / disattivata con il motivo / assente), come `row_login_as`.

## 12. Visibilità

| Elemento | P1 admin macchina | P2 admin workspace | P3 sviluppatore | P4 utente | P5 ruolo personalizzato | Meccanismo |
|---|---|---|---|---|---|---|
| Righe degli ambienti | tutte | quelle del workspace | idem | idem | solo le istanze a cui è limitato | `grant`, `visible_servers` |
| Stato, nome, tipo | sì | sì | sì | sì | sì | — |
| Branch, Repository ↗, CI | sì | sì | sì | — | con `code` | come la striscia |
| Striscia delle card | sì | sì | sì | — (la riga resta, senza card e senza spazio vuoto) | con `code`; senza, come P4 | `code` (route dei deploy) |
| Card «disponibile» + Aggiorna… | sì | sì | sì | — | con `code` | `manual_only`, `available_now` |
| Torna qui… (test, staging) | sì | sì | sì | — | con `code` | `can_back` |
| Torna qui… (produzione) | sì, col nome scritto | sì, col nome scritto | D2 | — | con `code` (D2) | `can_back` + D2 |
| Ricostruisci… | sì | sì | — | — | con `stagings` e la produzione fra le sue | `is_admin_here`, `denied.stagings`, `denied.rebuild` |
| Apri ↗ | sì | sì | sì | sì | sì | `instance_link` |
| Menu › Entra come… | sì | sì | sul test | — | `login_as`, disattivata senza | UX-013 §B3 |
| Menu › Log | sì | sì | sì | sì | `logs`, disattivata senza | `denied.logs` |
| Menu › Backup adesso… | sì | sì | sì | — | `backup`, disattivata senza | `can_act` |
| Menu › Shell | sì | — | sì se non `scoped` | — | — | `can_act and not scoped` |
| Menu › Indirizzi… | sì | sì | — | — | `names`, disattivata senza | `addresses[...].can` |
| Nome della macchina delle staging altrove | sì | «su un'altra macchina» | come il suo `scoped` | idem | idem | `customer_view` |
| «Altri N ›» | sì | sì | sì | — | con `code` | Moduli › Deploy |

## 13. HIDE / DEFER

| Cosa | Dove va | Rete di sicurezza |
|---|---|---|
| Filtro degli ambienti | compare oltre 8 righe | — |
| Intestazioni Produzione / Staging / Sviluppo | la pastiglia del tipo e l'ordine fisso | — |
| Card oltre la 3ª (2ª, 1ª) | «Altri N ›» → Moduli › Deploy | in corso, disponibile e adesso restano sempre visibili |
| Commit prima → dopo, conteggi «3 commit · 1 tolto» | `title` della card; tabella di Moduli › Deploy | — |
| Backup fatto prima del deploy | Moduli › Deploy e il dialogo di «Torna qui…» | il dialogo dice sempre se il backup c'è ancora |
| Versione di Odoo | riga dei fatti della testata | — |
| Chi ha avviato il deploy (`by`) | `title` della card; colonna di Moduli › Deploy | se manca l'autore, `by` è il testo della card |
| Interruttore della CI, webhook, segreto | Moduli › Aggiornamenti del codice (il segno «CI accesa/spenta» è un link) | — |
| Scadenza della staging («sarà eliminata il…») | non c'è (D5) | — |
| Riavvia, Ferma, Aggiorna, Porta in produzione… | testa della pagina dell'ambiente | — |
| Card nella lista Istanze e nella lista Workspace | — | un deploy fallito va alla campanella e a «N da guardare» |
| psql, Claude | «Altro ▾» della pagina dell'ambiente | — |

## 14. Vuoto, attesa, dati densi

- **Ambiente senza deploy**: la card «Il codice di adesso» (§6) e accanto, in `.small muted`:
  - produzione: «Nessun deploy ancora: il primo è un Aggiorna fatto a mano. Una produzione non si aggiorna da sola.»;
  - test e staging: «Nessun deploy ancora: il primo è un Aggiorna, un push con la CI accesa o un Torna a un deploy.»
- **Ambiente senza repository** (codice dal pacchetto o dal template): nessuna card; «Codice dal template: nessun repository, quindi nessun deploy. Si aggiunge un repository da Moduli › Codice.» (link).
- **Produzione senza test e senza staging**: una riga sola, più l'empty-state di oggi delle staging.
- **Attesa**: le righe (colonna sinistra) arrivano con la pagina, dal server; le card arrivano dopo, con lo scheletro di 3 card per riga (o 1 sul telefono). **Le card compaiono in < 2 s** su una produzione con test, 3 staging e 100 deploy ciascuna. Una staging altrove compare entro 5 s o diventa la sua riga d'errore. La striscia si rilegge alla fine di un job dell'ambiente e ogni 60 s con la scheda del browser visibile; mai con la scheda nascosta.
- **Dati densi**: messaggi lunghi troncati a una riga (intero nel `title`); errori troncati a 2 righe; con più di 8 ambienti il filtro (§4.2).

## 15. Il necessario

- **Stato del sistema**: il pallino con la parola per ogni ambiente; la card «in corso» con il link al job; la card «adesso»; l'esito con parola e segno; «Deploy non letti: <motivo>» per una macchina muta; lo scheletro durante l'attesa.
- **Annulla**: la sezione non cambia niente da sola. «Torna qui…», «Aggiorna…», «Ricostruisci…», «Backup adesso…» aprono i dialoghi di oggi, con Annulla ed Esc che non fanno partire nessuna richiesta. Un deploy si annulla con «Torna qui…» sulla card di prima, che è il punto di questa vista.
- **Prevenzione dell'errore**: «Torna qui…» assente su card fallite, disponibili e su quella di adesso; il dialogo dice prima cosa cambia, che il database **non** torna indietro e cosa farà il prossimo push; in produzione il nome scritto (D2); voci disattivate con il motivo quando il ruolo o lo stato già lo dicono.
- **Messaggio d'errore**: la prima riga dell'errore del job sulla card fallita, tradotta, con «Apri il job ›»; i rifiuti dei dialoghi passano da `tr` con il rimedio (quelli di `back_plan`: «cambia prima il branch», «ha modifiche non salvate»).
- **Aiuto**: `title` su ogni pulsante, voce di menu, segno CI, link Repository, card; gli empty-state dicono cosa fa il primo deploy.

## 16. Pulsanti e link (`title` scritto dalla route, in inglese con l'italiano negli i18n)

| Elemento | Tipo | Etichetta | `title` |
|---|---|---|---|
| Apri (card di adesso) | secondario, parte del diviso | Apri ↗ | «Apre Odoo di <ambiente> in una scheda nuova» |
| freccia del diviso | `summary` | ▾ | «Le altre cose da fare su <ambiente>: entrare come un utente, il log, un backup, la shell, gli indirizzi» |
| Torna qui… | secondario `.small` | Torna qui… | il testo di oggi di «Torna a questo deploy…»; in produzione in più: «Chiede il nome dell'istanza scritto.» |
| Aggiorna… (card disponibile) | secondario `.small` | Aggiorna… | il `title` di oggi di «Aggiorna» della testa |
| Apri il job | link | Apri il job › | «Apre il job di questo deploy: ogni passo, con l'errore» |
| messaggio del commit | link ↗ | <messaggio> | «Apre il commit <sha breve> di <repository> in una scheda nuova» |
| Altri N | link | Altri N › | «Apre tutti i deploy di <ambiente> in Moduli › Deploy, con il backup di prima di ognuno» |
| CI | link | CI accesa / CI spenta / a mano | «La CI di <ambiente>: …; si accende e si spegne in Moduli › Aggiornamenti del codice» |
| Repository | link ↗ | Repository ↗ | «Apre il branch <branch> di <repository> in una scheda nuova» |
| Ricostruisci… | secondario `.small danger` | Ricostruisci… | il testo di oggi della pagina della staging |

## 17. Criteri di accettazione (Playwright)

**Dove**: la build sulla coppia **lab3/lab4** con il pannello locale avviato dal worktree; l'aspetto finale sul pannello centrale con lab1/lab2 dopo il rilascio.
**Dati**: su lab3 una produzione Odoo 19 con 2 repository, test e 2 staging (una ferma), una terza staging su lab4. Produzione: 6 deploy (1 fallito, 1 con 2 repository), 1 commit «disponibile». Test: CI accesa, 4 deploy. Staging 1: CI spenta, 0 deploy. Un ruolo personalizzato con preset sviluppatore senza `login_as` né `backup`.
**Persone**: P1, P2, P3, P4, P5. Nessun gruppo developer nelle schermate del cliente. 1440×900 e 375×812, IT, tema chiaro e scuro.

1. **Righe**:
   - `[data-env]` nella sezione = 5, in quest'ordine di `data-env-kind`: production, test, staging, staging, staging-elsewhere;
   - la produzione ha «a mano», il test «CI accesa», la staging 1 «CI spenta»: ognuno è un `a` verso Moduli › Aggiornamenti del codice.
2. **Card**:
   - a 1440 px, nella riga del test: 3 `.deploy-card:visible` e 1 link «Altri 1 ›»; i deploy contati dalla route di Moduli › Deploy sono 4 = 3 + 1;
   - le card di ogni riga sono in ordine di `data-finished` decrescente, dopo le speciali;
   - la riga della produzione ha come prima card quella con `data-kind=available`, e dentro 1 pulsante «Aggiorna…»;
   - per ogni riga, esattamente 1 card con la pastiglia «adesso»;
   - la card fallita ha la pastiglia con «fallito», il testo dell'errore non vuoto e 1 `a` «Apri il job ›»; 0 «Torna qui…»;
   - la card con 2 repository contiene «+ 1 repository»;
   - ogni card con età ha un `title` che rispetta `\d{2}/\d{2}/\d{4} \d{2}:\d{2}`.
3. **Vuoto**: la staging 1 ha 1 card «Il codice di adesso» e il testo che contiene «Nessun deploy ancora».
4. **Il menu della card di adesso** (riga del test):
   - P1: le voci sono, in ordine, [Entra come…, Log, Backup adesso…, Shell, Indirizzi…];
   - P2: 0 voci «Shell»;
   - P3: [Entra come…, Log, Backup adesso…, Shell] se della macchina, senza Shell se del workspace;
   - P5: «Entra come…» e «Backup adesso…» `disabled`, ognuna in uno `span[title]` con il testo di `refused`;
   - P4: 0 card, quindi 0 menu; «Apri ↗» c'è come link della riga.
5. **Un posto solo**:
   - sulla Panoramica della produzione, `.note-box[data-available]` fuori dalla sezione = 0;
   - nella tabella degli ambienti, `[data-login-as]:visible` fuori da un menu aperto = 0;
   - `.primary:visible` nella scheda Panoramica = 1 («+ Nuova staging»); 0 con un job in corso;
   - le schede della pagina sono lo stesso numero di prima (6 sulla produzione).
6. **Torna qui…**:
   - su una card del test: apre `dialog[open]` di «Torna a…»; Annulla → 0 richieste POST;
   - su una card della produzione: il pulsante di conferma è `disabled` finché il campo non contiene il nome dell'istanza (D2);
   - dopo la conferma sul test, la pagina va al job, e alla fine del job la card «adesso» del test è quella scelta.
7. **Ricostruisci…**: sulla riga della staging di lab3, P1 → apre il dialogo con 2 radio; sulla riga di lab4 è `disabled` con `span[title]` che contiene «lab4»; P3 → 0 pulsanti.
8. **Macchina muta**: con lab4 fermo, entro 7 s la riga della staging di lab4 dice «Deploy non letti» con il motivo; le altre righe hanno le loro card.
9. **Riservatezza di P2**: il DOM e le risposte JSON della sezione contengono 0 occorrenze di «lab3», «lab4», «/var/», «/srv/»; `by` nelle `title` è nelle parole di `customerview`.
10. **Attesa**: con 100 deploy per ambiente, le card di tutte le righe di lab3 compaiono in < 2 s dal caricamento, e prima si vede lo scheletro. Con la scheda del browser nascosta: 0 richieste in 120 s.
11. **Telefono, 375 px**:
    - `scrollWidth` ≤ 375;
    - ogni riga ha 1 `.deploy-card:visible`; per la produzione è quella «disponibile»;
    - «Apri ↗», la freccia del menu e «Ricostruisci…» sono alti almeno 44 px.
12. **Titoli**: ogni `button`, `summary`, `a` della sezione e voce di menu ha un `title` (o uno `span[title]`) non vuoto e diverso dal testo; 0 chiamate ad `alert`, `confirm`, `prompt`.
13. **Non cambia niente**: caricare la pagina, aprire e chiudere i menu e aspettare 2 riletture fa 0 richieste POST, PUT o DELETE.

## 18. Smell che fanno FAIL (per `odoo-ux-tester`)

- Una scheda «Build» o «Deploy» nuova nella pagina dell'istanza.
- Lo stesso commit disponibile detto due volte nella Panoramica (card e `note-box`).
- La versione di Odoo su ogni card.
- Un esito affidato al solo colore; una staging ferma o una CI spenta segnate come allarme.
- Più di una card «adesso» in una riga, o la card in corso / disponibile nascosta dietro «Altri N».
- «Torna qui…» su una card fallita o disponibile; «Torna qui…» in produzione senza il nome scritto (con D2).
- Un interruttore della CI nella riga.
- Card o strisce nella lista Istanze o nella lista Workspace.
- Una voce del menu attiva dove il server la rifiuterebbe per un motivo già noto.
- Scorrimento orizzontale a 375 px.
- Gli screenshot presi solo come admin della macchina, o con il gruppo developer.

## 19. Decisioni di Carlo (la raccomandata per prima)

1. **D1 — Dove vive.**
   - **Raccomandata: la tabella Ambienti della Panoramica diventa «Ambienti e deploy».** Le schede restano 6 sulla produzione (7 su una staging). Esempio: acme con test e 2 staging: oggi per sapere com'è andato l'ultimo deploy dei 4 ambienti servono 4 pagine × (1 clic su Moduli + scorrere fino a Deploy) = circa 8 clic; dopo, 0 clic dalla pagina della produzione.
   - Alternativa: una scheda «Deploy» nuova, accanto alla Panoramica: le schede diventano 7 sulla produzione e 8 sulla staging (il tetto di UX-006 è 5), e la tabella Ambienti resta un secondo elenco degli stessi ambienti.
   - Alternativa: una sezione nella pagina del workspace, con tutte le produzioni: con 5 produzioni sono circa 20 righe e 60 card in una pagina.
2. **D2 — «Torna qui…» su una produzione.**
   - **Raccomandata: lo sviluppatore lo può ancora fare, ma scrivendo il nome dell'istanza**, come per il ripristino. Esempio: acme con 5 sviluppatori; uno torna indietro per sbaglio di 3 commit: oggi basta un clic su «Torna indietro» nel dialogo, dopo serve scrivere «acme-odoo».
   - Alternativa: in produzione solo gli admin (macchina o workspace), con il nome scritto. Uno sviluppatore che deve annullare un deploy rotto alle 22 aspetta un admin.
   - Alternativa: com'è oggi, senza nome scritto.
3. **D3 — Moduli › Deploy.**
   - **Raccomandata: resta, come storico intero (100 per ambiente), e «Altri N ›» porta lì.** In Panoramica 3 card per riga, in Moduli tutte le righe con il backup di prima.
   - Alternativa: va via, e «Altri N ›» apre un dialogo con tutte le card. 100 card in un dialogo sono una griglia da scorrere; il backup di prima e i secondi senza servizio non ci stanno.
4. **D4 — Un job del codice fallito che non ha spostato niente.**
   - **Raccomandata: diventa una card rossa** (un tipo «fallito senza deploy» nel file dei deploy, solo per i job del codice: Git pull, push, aggiornamento automatico, Torna a). Esempio: un push sul test con un conflitto: oggi la striscia del test mostra il deploy di ieri «riuscito» e «adesso», come se il push non ci fosse stato; la sola traccia è la campanella. Dopo, la prima card è «✕ fallito» con l'errore.
   - Alternativa: come oggi, solo i job che hanno spostato un commit. La striscia dice «com'è il codice», non «cosa è successo».
5. **D5 — Le staging che scadono.**
   - **Raccomandata: niente riga «sarà eliminata il…»**, perché in OmniaCLI le staging non scadono: la riga dice «copia del 21/11/2026». Esempio: una staging del 01/10 occupa il suo disco finché qualcuno non la elimina, e la riga lo mostra con la sua data.
   - Alternativa: una regola di scadenza per workspace (per esempio 30 giorni dalla copia, avviso 3 giorni prima), con la riga «Sarà eliminata il 21/12/2026». È una funzione nuova, con un job di pulizia: va prima a `software-architect` per un AD.
6. **D6 — L'età sulle card.**
   - **Raccomandata: relativa («2 giorni fa»), con la data esatta nel `title`**, come in Odoo.sh: sulla striscia si confrontano età, non date. È l'unica eccezione alla regola 3 di UX-006, e vale solo per le card.
   - Alternativa: la data `gg/mm/aaaa hh:mm` di `whenText`, come nel resto del pannello.

## Non verificato

- Il tempo della richiesta unica con 5 ambienti e 100 deploy ciascuno: `available_now` fa un `git merge-base` per repository della produzione, `deploy_rows_for` legge la cartella dei backup per ambiente. Se supera 2 s, le card vanno in cache con la loro data.
- Se il dialogo di «Torna a questo deploy…» si stacca da `_deploys.html` senza cambiare il suo comportamento in Moduli: oggi legge l'API dall'attributo della sezione.
- Se una staging altrove passa i suoi deploy attraverso il collegamento con il ruolo giusto (il più basso fra chi guarda e il collegamento): non provato.
- Se `git log --format=%an` sui commit di un deploy vecchio dà ancora l'autore dopo un `reset --hard` (il commit può non essere più nella copia): in quel caso la card usa `by`.

## Tracciabilità

Richiesta di Carlo del 23/11/2026, in chat, con lo screenshot della pagina Builds di Odoo.sh. Non ci sono un `TS-NNN` né un `T<n>`/`HT<n>`.
- UX-006 §1-3 (schede, primario, date), §4.1 (Ambienti);
- UX-008 §2 regole 1-3, §5.1-5.2 (staging altrove, Ricostruisci);
- UX-009 §6 (stati);
- UX-013 §B2-B5 (Entra come… sulle righe: la tabella Ambienti cambia qui, §7 e §10).

Fonti nel codice:
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/server.html`: le schede (375-385), la `note-box` dei commit disponibili (327-331), la tabella Ambienti (403-434), le staging altrove (436-470), la `note-box` della staging con Ricostruisci (343-350), «Altro ▾» (225-300), `addresses_button` (116), `row_login_as_button` (123);
- `/home/carlo/Projects/OmniaCLI/omniacli/templates/_deploys.html`: la tabella e il dialogo «Torna a questo deploy…»;
- `/home/carlo/Projects/OmniaCLI/omniacli/deploys.py`: `Recorder.end` (cosa diventa un deploy), `changes` (niente autore), `KEEP`;
- `/home/carlo/Projects/OmniaCLI/omniacli/app.py`: `note_available` (12205), `deploy_rows_for` e `current` (12741), `available_now` (12770), `deploys_list` (12803), `manual_only` e `ci_on` (12010-12014), `instance_summary` e `compare_url` (2890), `denied` e `REBUILD_NOT_YOURS` (2565, 347);
- `/home/carlo/Projects/OmniaCLI/omniacli/stagings.py`: nessuna scadenza;
- `/home/carlo/Projects/OmniaCLI/omniacli/capabilities.py`: `CAPABILITIES`, `LEVELS`, le route dei deploy sotto `code` (188-197);
- `/home/carlo/Projects/OmniaCLI/omniacli/workspaceops.py`: `web_url`, `compare_url` (444-464);
- `/home/carlo/Projects/OmniaCLI/README.md`: «Stagings, deploy on push, monitoring» (2227), «Deploys» e «Back to this deploy…».

## Decise da Carlo (23/11/2026)

D1 nella Panoramica («Ambienti e deploy»), D4 un job del codice fallito senza commit spostati è una scheda rossa, D5 nessuna scadenza delle staging per ora. D2, D3, D6 con la raccomandata.
