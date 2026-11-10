"""Consumidor de q-notificaciones (sin filtro: recibe TODOS los eventos) -> tabla notificaciones."""
from botocore.exceptions import ClientError

from src.common import TABLA_NOTIFICACIONES, ahora_dt, iso, log, procesar_lote

MENSAJES = {
    'incidente.creado': 'Nuevo incidente {incidente_id}: {titulo}',
    'incidente.escalado_a_problema': 'El incidente {incidente_id} fue escalado a problema',
    'incidente.resuelto': 'El incidente {incidente_id} fue resuelto',
    'problema.creado': 'Nuevo problema {problema_id}',
    'problema.causa_raiz_identificada': 'Se identificó la causa raíz del problema {problema_id}',
    'problema.resuelto': 'El problema {problema_id} fue resuelto',
    'cambio.aprobado': 'El cambio {cambio_id} fue aprobado',
    'cambio.implementado': 'El cambio {cambio_id} fue implementado',
    'solicitud.creada': 'Nueva solicitud {solicitud_id}',
    'solicitud.cambio_estandar': 'La solicitud {solicitud_id} generó un cambio estándar',
    'solicitud.completada': 'La solicitud {solicitud_id} fue completada',
    'usuario.creado': 'Nuevo usuario {usuario_id}',
}


def construir_mensaje(evento, data):
    plantilla = MENSAJES.get(evento)
    if plantilla:
        try:
            return plantilla.format(**data)
        except (KeyError, IndexError, ValueError):
            pass
    return f'Evento {evento}'


def handler(event, context):
    return procesar_lote(event, guardar)


def guardar(sobre, record):
    data = sobre.get('data') or {}
    creado_en = sobre.get('timestamp') or iso(ahora_dt())
    evento_id = record['messageId']
    item = {
        'tenant_id': sobre['tenant_id'],
        'notificacion_id': f'{creado_en}#{evento_id}',
        'evento_id': evento_id,
        'evento': sobre['evento'],
        'origen': sobre.get('origen', 'desconocido'),
        'mensaje': construir_mensaje(sobre['evento'], data),
        'data': data,
        'leida': False,
        'creado_en': creado_en,
    }
    try:
        TABLA_NOTIFICACIONES.put_item(Item=item, ConditionExpression='attribute_not_exists(notificacion_id)')
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':  # duplicado: ya guardado
            log('INFO', accion='notificacion_duplicada', evento_id=evento_id)
            return
        raise
    log('INFO', accion='notificacion_guardada', tenant_id=sobre['tenant_id'], evento=sobre['evento'])
