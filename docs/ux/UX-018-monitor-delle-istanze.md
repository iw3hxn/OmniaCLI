# UX-018 — Il monitor di un'istanza: «sta bene? E se no, perché»

Stato: contratto di design, prima della build. Origine: Carlo, 26/11/2026: l'equivalente, per ogni istanza, della pagina Monitor di un branch di Odoo.sh. Lo implementa chi tocca la scheda Monitoraggio di `omniacli/templates/server.html` (`data-panel="monitor"`, le due sezioni di oggi), `omniacli/i18n/it/server.json`, `/api/metrics` e le route nuove dei dati (§9). Valgono UX-001…017, in particolare UX-006 §2 e §4.4 (la scheda, la sezione Database, il suo verdetto e le soglie D4) e UX-017 (token, `.period`, badge, scheletri, telefono). Questo contratto li applica e non li ripete.

**Prima della build serve un AD** (§9, AD-019): oggi un'istanza tiene solo l'ultima ora (un punto ogni 5 s); richieste, posta e conteggi del log non si misurano.

## 0. Gate

UI non banale: **sì**. Rischio: copiare per parità il muro di Grafana di Odoo.sh (circa 20 grafici, 3 torte, 10 tile). Il contratto tiene **un verdetto, quattro tile, sezioni chiuse** che si aprono da sole quando una causa le nomina.

## 1. Persone e lavoro primario

| Persona | Nel codice | Device |
|---|---|---|
| P1 Admin della macchina (sviluppatore del partner) | `is_admin`, non `scoped` | desktop 1440×900 |
| P2 Admin del workspace (l'IT del cliente) | `scoped`, admin del workspace, `customer_view` | desktop, telefono 390 |
| P3 Sviluppatore | ruolo `developer` | desktop |
| P4 Utente del workspace | ruolo `user` | telefono |

**Lavoro primario**: in 3 secondi, «l'istanza sta bene adesso? Se no, perché, e cosa si fa». Secondario: «stamattina era lenta, cos'è successo alle 10?» (il periodo, §4); come cresce lo spazio; se la posta parte.

## 2. Sopra la piega, in ordine

1. **Titolo e periodo** (§4). Nella scheda 0 `.primary` (UX-006 §3).
2. **Verdetto sull'adesso** (ultimi 15 minuti), `role="status"`, pallino e parola, mai solo colore:
   - «Sta bene» (`--ok`);
   - «Da guardare» (`--warn`), con una riga per causa (§3);
   - «Non risponde» (`--err`): 3 controlli di fila senza risposta, «da 10:12; il servizio dice: in funzione»;
   - «Ferma» (`--muted`): «Fermata il 26/11 alle 09:40: non si misura»;
   - «Ancora nessuna misura» (`--muted`, §7).

   Sotto, se nel periodo c'è stato un problema che adesso non c'è: «Nelle ultime 24 ore: non ha risposto per 6 minuti alle 03:12 · 2 worker fermati per memoria.» Ogni pezzo è un link alla sua sezione.
3. **Quattro tile sul periodo scelto**, ognuna con il confronto e la soglia in parole:

| Tile | Valore | Sotto |
|---|---|---|
| Risponde | 99,9% | «del tempo, nelle ultime 24 ore» |
| Tempo di risposta | 0,4 s | «metà delle pagine; 1 su 20 oltre 2,1 s» |
| Richieste non riuscite | 0,2% | «31 su 12.400; oltre l'1% è da guardare» |
| Memoria | 1,2 GiB | «di 4 GiB concessi; oltre l'85% è da guardare» (senza limite: «massimo del periodo 1,6 GiB») |

La CPU non è una tile: compare come causa quando supera la soglia, e nel grafico Carico.

## 3. Le cause del verdetto

Le soglie di CPU, memoria e disco sono **quelle di Opzioni › Monitoraggio** (avviso 90 / 85 / 90%, per 5 minuti di fila): la pagina e l'e-mail di avviso dicono la stessa cosa. Ogni riga porta il fatto con i numeri, la soglia, il rimedio e il link alla sezione, che si apre. Le righe visibili sono al più 5, poi «e altre N ▸».

| Causa | Quando | Riga (esempio) |
|---|---|---|
| Lenta | metà delle richieste oltre 1 s, o 1 su 20 oltre 3 s (D-N1) | «Le pagine rispondono in 1,8 s, 1 su 20 oltre 6 s: sopra 1 s si sente.» |
| Errori | oltre l'1% di richieste non riuscite | «Il 4% delle richieste non riesce (52 su 1.300): gli errori sono nel Log.» (link al Log filtrato) |
| Worker occupati | tutti occupati per 5 minuti | «Tutti i 4 worker occupati da 7 minuti: chi apre Odoo aspetta. Aggiungi worker in Impostazioni › Opzioni di Odoo.» |
| Memoria | oltre l'avviso, o almeno 1 worker fermato per memoria | «Memoria al 91% di 4 GiB; 2 worker fermati alle 10:14 e alle 10:20.» |
| Processore | oltre il 90% dei core concessi | «Processore al 96% dei 2 core concessi da 12 minuti.» |
| Database | soglie D4 di UX-006 | le righe di UX-006 §4.4 |
| Spazio | disco oltre il 90%, o pieno entro 14 giorni secondo la previsione | «Il disco sarà pieno verso il 9/12: il database cresce di 180 MB al giorno.» |
| Posta | almeno 1 e-mail non partita nelle 24 ore | «12 e-mail non partite oggi: il server di posta rifiuta l'accesso.» |

Rimedi per persona: P2 legge «chiedi al tuo fornitore» dove non può agire; P4 legge la causa senza il rimedio.

## 4. Il periodo

- **Il comando**: segmenti `.period`, lo stesso componente della Dashboard, con `aria-pressed`: **1 ora** (dal vivo, un punto ogni 5 s), **24 ore** (il predefinito), **7 giorni**, **14 giorni**. Da 24 ore in su è un punto ogni 5 minuti, con media e massimo. 14 giorni è quanto tiene la storia, come per la macchina.
- **Perché 24 ore come predefinito** (D-N2): la domanda tipica è «stamattina era lenta», e il periodo copre anche la notte dei backup e dei cron.
- **Cosa segue il periodo**: tile, grafici e la riga «Nelle ultime…». Il verdetto no, resta sull'adesso.
- **La scelta resta** per la sessione del browser (`sessionStorage`).
- **Aggiornamento automatico**: ogni 5 s con 1 ora, ogni minuto negli altri periodi, mai con la pagina nascosta. Nessun pulsante Aggiorna.
- **Storia più corta del periodo**: lo si dice, «Misurato dal 20/11».

## 5. Sotto la piega: sezioni chiuse

Le sezioni sono `details` chiusi. Il `summary` porta il titolo e la riga che le riassume, così anche chiusa la sezione informa. Una sezione nominata da una causa si apre da sola.

| Sezione | Riga da chiusa | Dentro (grafici e serie) | Chi |
|---|---|---|---|
| Carico | «Processore al massimo 64%, memoria al massimo 1,6 GiB» | «Processore, % dei core concessi»: Odoo · Database · Soglia di avviso (tratteggiata). «Memoria in uso»: Odoo · Database · Limite concesso | tutti |
| Richieste | «12.400 richieste, 0,2% non riuscite» | «Richieste al minuto»: Riuscite · Non riuscite · Rifiutate per troppe richieste (solo se più di 0). «Tempo di risposta»: Metà delle richieste entro · 1 su 20 oltre. «Le 10 pagine più lente»: Indirizzo · Richieste · Tempo medio · La più lenta | grafici a tutti; tabella a P1, P2, P3 |
| Database | il verdetto di UX-006 | la sezione di UX-006 §4.4 com'è | P1, P2, P3 |
| Spazio | «Database 3,2 GB · allegati 8,1 GB · +180 MB al giorno» | «Spazio occupato»: Database · Allegati e file; la previsione in una riga | P1, P2, P3 |
| Posta | «Inviate 340 · non partite 0 · ricevute 85 · respinte 2» | «E-mail in uscita al giorno»: Inviate · Non partite. «E-mail in entrata al giorno»: Ricevute · Respinte | P1, P2, P3 |
| Il log di Odoo | «3 errori e 41 avvisi nelle 24 ore» | colonne 24 ore e 7 giorni; righe: Errori · Avvisi · Worker fermati per memoria · Database fermato per memoria · Conflitti tra transazioni (riprovate) · Chiamate dalle API (XML-RPC e JSON-RPC). Ogni numero apre il Log filtrato | P1, P2, P3; la riga delle API solo P1 e P3 |

Nella tabella del log, una riga a zero in tutti e due i periodi non compare, tranne Errori e Avvisi; al suo posto una riga sola: «Nessun worker fermato per memoria e nessun conflitto in 7 giorni.»

## 6. Visibilità

### 6.1 Per persona

| Elemento | P1 | P2 | P3 | P4 | Meccanismo |
|---|---|---|---|---|---|
| Verdetto, periodo, 4 tile, Carico, grafici delle Richieste | sì | sì | sì | sì | — |
| Rimedio nelle cause | sì | sì, o «chiedi al tuo fornitore» | sì | — | `can_act` |
| Pagine più lente, Database, Spazio, Posta, Log | sì | sì | sì | — | `can_act` |
| Riga delle API | sì | — | sì | — | ruolo |
| PostgreSQL condiviso, nomi di macchina, cluster e host, altri database | sì | **mai**, né nel DOM né nel JSON | come il suo ambito | mai | `customer_view` |

### 6.2 Per dove sta il database (`pgreport.where`)

| | Dedicato (servizio del progetto) | Cluster della macchina | Remoto (`db_host`) |
|---|---|---|---|
| Serie «Database» in Carico | sì | no. P1 vede una serie tratteggiata «PostgreSQL del server, tutti i database»; gli altri la nota «Il database è nel PostgreSQL condiviso: il suo carico non si separa da quello degli altri database.» | no. La nota «Il database è su un'altra macchina: qui si leggono le sue statistiche, non il suo carico.» (P1 la vede con l'host) |
| Connessioni | sul massimo del servizio | «3 di questo database; 41 su 100 in tutto» | come il cluster, se si leggono |
| Statistiche non leggibili | il verdetto «Non leggibile: <motivo>» di UX-006 | idem | idem |

**Una fonte che manca è una frase al posto del grafico, mai un grafico vuoto o uno zero.** I due casi più frequenti:
- **Odoo non scrive un file di log**: la tile del tempo di risposta mostra «—», e Richieste e Log dicono «Le richieste si contano dal log di Odoo, che questa istanza non scrive in un file.» (P1 legge anche l'opzione da cambiare, `logfile`).
- **Staging**: Posta dice «Le e-mail di una staging sono trattenute: 14 trattenute nelle 24 ore.»

## 7. Vuoto, buchi, dati densi

- **Scheda sempre visibile** (D-N3): il verdetto c'è anche senza grafici. Senza dati: «Ancora nessuna misura: il monitor parte entro un minuto, il grafico delle 24 ore si riempie da qui.»
- **Un buco resta un buco**, mai uno zero, con il `title` «Nessuna misura dalle 03:10 alle 03:40».
- **Nessuna richiesta**: «Nessuna richiesta in questo periodo.»
- **Dati densi**: al più 300 punti per serie, il massimo come banda sopra la media; con 7 e 14 giorni l'asse è in giorni.
- **Attesa**: gli scheletri di UX-017 §5.12 nelle sezioni che leggono dal server.

## 8. Telefono, tema, dialoghi

- **Telefono, 390 px**: i 4 segmenti in una riga, alti 44; le tile 2 × 2; i grafici a tutta larghezza, alti 160, con la legenda sotto; le tabelle scorrono dentro la loro card; `scrollWidth` ≤ 390.
- **Chiaro e scuro**: serie `--series-*` / `--cat-*`, soglia `--warn` tratteggiata con la sua etichetta, nessun colore fisso.
- **Dialoghi**: nessun `alert`, `confirm` o `prompt`. «Ferma» di una query usa `omniaAsk` come oggi; «Rileggi» resta secondario.
- **Accessibilità dei grafici**: ogni grafico ha `role="img"` e un `aria-label` che lo riassume («Processore di acme nelle ultime 24 ore: massimo 64% alle 10:12»).

## 9. Contratto dei dati (per l'AD; i nomi li decide chi implementa)

- **Storia di ogni istanza, 14 giorni, un punto ogni 5 minuti**: CPU, memoria, richieste, non riuscite, tempi (mediana e 1 su 20), worker occupati. Come `metricshistory` fa per la macchina.
- **Richieste, tempi, pagine e conteggi del log**: dalle righe del log di Odoo, lette in coda come fa `logtail`, mai il file intero.
- **Posta**: query fisse in sola lettura sul database dell'istanza, come `PG_REPORT`, ogni 5 minuti.
- **Spazio**: un punto al giorno, per il database e per gli allegati.
- Le risposte per P2 passano da `customer_view`.

Domande per `software-architect`:
1. Chi legge il log di un'istanza venv, e quello di un'istanza spostata su un'altra macchina (AD-014)?
2. Quanto costa leggerlo ogni 5 minuti su 20 istanze?
3. Dove va la storia di un'istanza che si sposta?

## 10. Il necessario

- **Stato**: verdetto con la parola, riga del periodo, «Misurato dal», testo accanto allo scheletro dopo 5 s.
- **Annulla**: la scheda non cambia niente; «Ferma» ha la sua conferma con Annulla.
- **Prevenzione dell'errore**: le soglie hanno la stessa fonte delle e-mail di avviso; nessuno zero inventato.
- **Messaggio d'errore**: una fonte che non si legge dice il motivo in italiano e il rimedio.
- **Aiuto**: `title` su tile, segmenti, serie e `summary`; la soglia scritta sotto ogni tile.

## 11. HIDE: cosa di Odoo.sh non si mostra

| Odoo.sh | Qui | Perché |
|---|---|---|
| IOPS e 4 tile IO | — (P1 li ha nella Dashboard della macchina) | metrica da ingegneri; un disco lento si vede in «Letto dalla memoria» e nel tempo di risposta |
| CPU divisa in Cron, HTTP, Other | una serie «Odoo» | la divisione non cambia il rimedio |
| Toast | dentro «Database» | gergo di PostgreSQL |
| Tre torte | Spazio, più «Le tabelle più grandi» in Database › Dettagli | una torta non mostra la crescita; la tabella c'è già |
| Richieste contemporanee massime | la causa «Tutti i worker occupati» | il numero da solo non dice cosa fare |
| Richieste al secondo, totale delle richieste riuscite | riga chiusa di Richieste | duplicato |
| Richieste rifiutate sempre visibili | solo se più di 0 | — |
| ‹ ›, zoom indietro, Aggiorna | 4 segmenti e aggiornamento automatico | Hick |
| XML-RPC e JSON-RPC per tutti | solo P1 e P3 | — |
| Limiti di memoria e conflitti a zero | una riga riassuntiva | — |

## 12. Criteri di accettazione (Playwright)

**Dove**: lab3 e lab4, con il pannello locale dal worktree. Mai sui server dei clienti.
**Dati**: su lab3 `acme-odoo` con database dedicato, `acme-shop` sul cluster della macchina, un'istanza con `db_host` su lab4 e una staging. Uno script di carico porta 1 richiesta su 20 oltre 3 s e il 2% di errori 500.
**Persone**: P1, P2, P3, P4, nessuna col gruppo developer. 1440×900 e 390×844, IT, tema chiaro e scuro.

1. A riposo un `[role=status]` dice «Sta bene». Sotto carico, entro 2 minuti, dice «Da guardare», con una riga che contiene «1 su 20» e «3 s» e una che contiene «non riesce».
2. Le tile sono esattamente 4, e ogni sottotitolo contiene un numero o una soglia.
3. Al primo carico il segmento premuto è «24 ore». «7 giorni» cambia i sottotitoli delle tile e non il verdetto; dopo un reload resta «7 giorni».
4. A riposo i `details[open]` sono 0. Con la causa memoria, Carico è `[open]`.
5. Con tutto chiuso, a 1440×900, la scheda è alta al più 900 px.
6. Cluster (`acme-shop`): per P2, 0 serie «Database» e la nota con «condiviso»; per P1, la serie «PostgreSQL del server».
7. Remoto: la nota con «su un'altra macchina»; per P2, 0 occorrenze dell'host.
8. P2: DOM e JSON contengono 0 occorrenze di «lab3», «lab4», `/var/`, `/srv/` e del nome del cluster.
9. P4: 0 sezioni Database, Spazio, Posta e Log; 0 rimedi.
10. Istanza fermata apposta: «Ferma» e 0 «Non risponde». Container di Odoo fermato con il servizio acceso: «Non risponde» entro 4 minuti.
11. Con OmniaCLI fermo 15 minuti, il grafico di 1 ora ha un buco senza punti a 0.
12. Senza file di log: la tile del tempo di risposta mostra «—» e c'è la frase di §6.2.
13. Staging: la sezione Posta contiene «trattenute».
14. A 390 px: `scrollWidth` ≤ 390, segmenti alti ≥ 44, tile 2 per riga.
15. `.primary:visible` = 0 e 0 `alert`, `confirm`, `prompt`. Ogni `button`, `summary`, tile e segmento ha un `title` non vuoto e diverso dal testo; ogni `svg.chart` ha un `aria-label`.
16. 0 occorrenze visibili di «IOPS», «TOAST», «JSON-RPC» fuori dalla riga delle API di P1 e P3. Schermate dello stesso stato in tema chiaro e scuro.

## 13. Smell che fanno FAIL

- Un grafico vuoto o uno zero dove manca la fonte.
- Più di 4 tile, una torta, un pulsante Aggiorna o un primario.
- Il verdetto detto con il solo colore.
- Una soglia scritta in modo diverso da quella dell'e-mail di avviso.
- Schermate prese solo come admin, o con il gruppo developer.

## 14. Da decidere (la raccomandata per prima)

1. **D-N1 — La soglia di «lenta».** Raccomandata: metà delle richieste oltre 1 s, o 1 su 20 oltre 3 s, negli ultimi 15 minuti. Esempio: 1.300 richieste, 65 oltre 3 s → «Da guardare». Alternativa: due soglie in Opzioni › Monitoraggio, accanto alle altre.
2. **D-N2 — Il periodo predefinito.** Raccomandato: 24 ore. Alternativa: 1 ora, come oggi; non mostra il «stamattina alle 10».
3. **D-N3 — La scheda Monitoraggio sempre visibile.** Cambia UX-006 §2. Raccomandata: sì, «Non risponde» va detto anche senza grafici. Alternativa: com'è oggi, compare al primo dato.

## Non verificato

- Che le righe di accesso HTTP del log di Odoo portino i tempi su ogni versione supportata, con `log_level` info.
- Che esista un limite di richieste davanti alle istanze: senza, la serie «Rifiutate» non compare mai.

## Tracciabilità

Richiesta di Carlo del 26/11/2026, con la pagina Monitor di Odoo.sh. Base: UX-006 §1-2, §4.4 e D4; UX-017 §2, §5.12, §5.3.

Fonti nel codice: `omniacli/templates/server.html` (369, 675-729, 2448-2485); `omniacli/app.py` (3776-3820); `omniacli/monitor.py` (`DOWN_AFTER` = 3); `omniacli/config.py` (1995-1997, le soglie); `omniacli/pgreport.py` (`where`); `omniacli/metricshistory.py`; `omniacli/templates/index.html` (164-170, `.period`).
