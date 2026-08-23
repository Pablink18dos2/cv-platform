# Process

- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly

## Roles

- **PM** - grooms a task before anyone implements it, follows `_docs/team/pm.md`
- **Engineer** - implements one groomed task, follows `_docs/team/software-engineer.md`
- **QA** - checks the result against the acceptance criteria, follows `_docs/team/qa-engineer.md`

Each role runs in its own session. The agent that wrote the code does not judge the code.

## Orchestrator

The main session is the orchestrator. It launches the PM, the engineer and QA as
subagents. It does not groom, implement or test itself.

### Lifecycle

1. Pick the next open issue from the backlog
2. PM grooms it
3. Engineer implements it
4. QA verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, close the issue
7. Repeat until the backlog is empty

### Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS

## Dos niveles de ceremonia

Decidido el 2026-08-23, con el coste de los dos primeros ciclos ya medido: el #2 costó
386 k tokens de subagente y el #3 costó 1,06 M, porque un FAIL obliga a repetir la vuelta
entera de ingeniero y QA. El ciclo completo encuentra cosas de verdad, pero cuesta, y no
todas las tareas se juegan lo mismo.

**Ciclo completo (PM → Engineer → QA, cada uno en su sesión)** para las tareas donde un error
es caro y difícil de ver a simple vista:

- Lógica de negocio: los analizadores (#10 tráfico, #11 aparcamiento)
- Contratos que otros consumen: la API y su `openapi.yaml` (#12)
- Cualquier tarea cuyo resultado sea un número que alguien vaya a creerse

**Ciclo ligero (el orquestador groomea, implementa y verifica contra los criterios)** para
las tareas mecánicas, donde el fallo se ve al ejecutar: connectors, adaptadores, empaquetado
(#4, #7, #15) y las tareas pequeñas de seguimiento.

### Lo que el ciclo ligero pierde, dicho claro

En el ciclo ligero quien escribe el código es quien lo juzga, que es exactamente lo que el
ciclo completo existe para evitar. Se acepta a sabiendas y solo donde el fallo es visible al
ejecutar. A cambio, en el ciclo ligero es obligatorio:

- Recorrer los acceptance criteria uno a uno y decir qué comando demuestra cada uno.
- Pegar la salida real de los comandos, no describirla.
- Si aparece una duda sobre si algo está bien, se sube a ciclo completo. La duda es la señal.

### Los criterios se corrigen en el issue, nunca en el código

Los dos FAIL del issue #3 fueron defectos del enunciado, no del código: un mensaje de error
que nombraba un campo con valor por defecto (y un campo con default no puede faltar nunca), y
un presupuesto de tiempo por test que no contaba con que un subproceso que importa pydantic
cuesta ~1 s de suelo. Los dos se arreglaron editando el issue y dejando una nota fechada
explicando por qué. Es la forma barata de arreglarlo; hacerlo en el código deja el enunciado
mintiendo para siempre.
