"""Consumidor de q-incidentes. Evento: problema.resuelto -> incidentes vinculados pasan a RESUELTO."""
from src.common import TABLA_INCIDENTES, actualizar, ahora_dt, iso, log, procesar_lote, publicar_evento


def handler(event, context):
    return procesar_lote(event, procesar)


def procesar(sobre, record):
    if sobre['evento'] != 'problema.resuelto':
        log('INFO', accion='evento_ignorado', evento=sobre['evento'])
        return
    tenant_id = sobre['tenant_id']
    data = sobre.get('data') or {}
    problema_id = data.get('problema_id')
    incidentes_ids = data.get('incidentes_ids') or []
    if not incidentes_ids:
        log('INFO', accion='problema_sin_incidentes', tenant_id=tenant_id, problema_id=problema_id)
        return
    for incidente_id in incidentes_ids:
        resolver(tenant_id, str(incidente_id), problema_id)


def resolver(tenant_id, incidente_id, problema_id):
    if not incidente_id.startswith(f'inc-{tenant_id}-'):  # nunca tocar datos de otro tenant
        log('ERROR', accion='incidente_de_otro_tenant', tenant_id=tenant_id, incidente_id=incidente_id)
        return
    clave = {'tenant_id': tenant_id, 'incidente_id': incidente_id}
    inc = TABLA_INCIDENTES.get_item(Key=clave).get('Item')
    if not inc:
        log('INFO', accion='incidente_no_existe', tenant_id=tenant_id, incidente_id=incidente_id)
        return
    if inc['estado'] in ('RESUELTO', 'CERRADO'):  # idempotencia: SQS puede entregar duplicados
        log('INFO', accion='incidente_ya_resuelto', tenant_id=tenant_id, incidente_id=incidente_id)
        return

    momento = iso(ahora_dt())
    cambios = {
        'estado': 'RESUELTO',
        'estado_creado': f"RESUELTO#{inc['creado_en']}",
        'resuelto_en': momento,
        'actualizado_en': momento,
    }
    if problema_id:
        cambios['problema_id'] = problema_id
    actualizar(TABLA_INCIDENTES, clave, cambios, condicion={'estado': inc['estado']})
    publicar_evento('incidente.resuelto', tenant_id, {
        'incidente_id': incidente_id, 'titulo': inc['titulo'], 'problema_id': problema_id,
        'resuelto_por': 'problema',
    })
    log('INFO', accion='incidente_resuelto_por_problema', tenant_id=tenant_id, incidente_id=incidente_id,
        problema_id=problema_id)
