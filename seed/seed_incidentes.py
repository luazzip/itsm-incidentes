"""
Seed: 10,000 incidentes por tenant (acme, globex, initech) = 30,000 en itsm-incidentes-<stage>-incidentes.
Uso:
  python3 seed/seed_incidentes.py              -> stage dev, los 3 tenants
  python3 seed/seed_incidentes.py dev acme     -> solo acme
"""
import random
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from faker import Faker

STAGE = sys.argv[1] if len(sys.argv) > 1 else 'dev'
TENANTS = sys.argv[2].split(',') if len(sys.argv) > 2 else ['acme', 'globex', 'initech']
POR_TENANT = 10000

# SUPUESTO (confirmar con Integrante 5 segun su seed de usuarios):
# usr-<tenant>-00001 = ADMIN, 00002..00050 = AGENTE, 00051..10000 = SOLICITANTE
AGENTES = range(2, 51)
SOLICITANTES = range(51, POR_TENANT + 1)

IMPACTOS = ('ALTO', 'MEDIO', 'BAJO')
URGENCIAS = ('ALTA', 'MEDIA', 'BAJA')
PRIORIDADES = ('P1', 'P2', 'P3', 'P4')
SLA_HORAS = {'P1': 4, 'P2': 8, 'P3': 24, 'P4': 72}
ESTADOS = ('NUEVO', 'ASIGNADO', 'EN_PROGRESO', 'RESUELTO', 'CERRADO')
PESOS_ESTADO = (15, 10, 15, 15, 45)  # 15% nuevos, 25% en curso, 60% resueltos/cerrados
TITULOS = {
    'RED': ['VPN no conecta', 'Sin internet en el piso {n}', 'WiFi intermitente en sala de reuniones {n}',
            'Lentitud en la red corporativa'],
    'HARDWARE': ['Laptop no enciende', 'Impresora del piso {n} atascada', 'Monitor sin señal',
                 'Teclado con teclas dañadas'],
    'SOFTWARE': ['Error al abrir el ERP', 'Excel se cierra inesperadamente', 'Actualización de Windows fallida',
                 'El antivirus bloquea una aplicación'],
    'ACCESOS': ['Usuario bloqueado en el dominio', 'Restablecer contraseña de red', 'Sin permisos en carpeta compartida',
                'No puede ingresar al sistema de RRHH'],
    'CORREO': ['No llegan correos externos', 'Buzón de correo lleno', 'Outlook no sincroniza',
               'Correos legítimos llegan a spam'],
    'OTROS': ['Teléfono IP sin tono', 'Proyector de la sala {n} no funciona', 'Consulta sobre equipo nuevo',
              'Soporte para videoconferencia'],
}
AREAS = ['Finanzas', 'Ventas', 'RRHH', 'Logística', 'TI', 'Marketing', 'Operaciones', 'Legal']
DETALLES = ['Ocurre desde esta mañana.', 'Ya reinició el equipo y persiste.', 'Afecta a varios usuarios del área.',
            'Ocurre de forma intermitente.', 'Necesita solución urgente para una reunión.', 'Adjunta captura del error.']

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
tabla = dynamodb.Table(f'itsm-incidentes-{STAGE}-incidentes')
contadores = dynamodb.Table(f'itsm-incidentes-{STAGE}-contadores')
fake = Faker('es_ES')


def iso(momento):
    return momento.strftime('%Y-%m-%dT%H:%M:%SZ')


def generar(tenant):
    rnd = random.Random(f'2026-{tenant}')  # semilla fija: siempre los mismos datos
    fake.seed_instance(f'2026-{tenant}')
    hoy = datetime.now(timezone.utc).replace(microsecond=0)
    # Fechas en los ultimos 12 meses, ordenadas: inc-<t>-000001 es el mas antiguo
    fechas = sorted(hoy - timedelta(seconds=rnd.randint(0, 365 * 24 * 3600)) for _ in range(POR_TENANT))
    for n, creado in enumerate(fechas, start=1):
        categoria = rnd.choice(list(TITULOS))
        titulo = rnd.choice(TITULOS[categoria]).format(n=rnd.randint(1, 12))
        impacto = rnd.choices(IMPACTOS, weights=(15, 45, 40))[0]
        urgencia = rnd.choices(URGENCIAS, weights=(20, 45, 35))[0]
        prioridad = PRIORIDADES[min(IMPACTOS.index(impacto) + URGENCIAS.index(urgencia), 3)]
        estado = rnd.choices(ESTADOS, weights=PESOS_ESTADO)[0]
        solicitante = f'usr-{tenant}-{rnd.choice(SOLICITANTES):05d}'
        c = iso(creado)
        item = {
            'tenant_id': tenant,
            'incidente_id': f'inc-{tenant}-{n:06d}',
            'titulo': titulo,
            'descripcion': f'{fake.name()} del área de {rnd.choice(AREAS)} reporta: {titulo.lower()}. {rnd.choice(DETALLES)}',
            'categoria': categoria,
            'impacto': impacto,
            'urgencia': urgencia,
            'prioridad': prioridad,
            'estado': estado,
            'solicitante_id': solicitante,
            'escalado': False,
            'adjuntos': [],
            'sla_vence_en': iso(creado + timedelta(hours=SLA_HORAS[prioridad])),
            'creado_en': c,
            'estado_creado': f'{estado}#{c}',
            'solicitante_creado': f'{solicitante}#{c}',
        }
        actualizado = creado
        if estado != 'NUEVO':
            agente = f'usr-{tenant}-{rnd.choice(AGENTES):05d}'
            item['asignado_a'] = agente
            item['asignado_creado'] = f'{agente}#{c}'
            actualizado = min(creado + timedelta(minutes=rnd.randint(5, 240)), hoy)
        if estado in ('RESUELTO', 'CERRADO'):
            resuelto = min(actualizado + timedelta(hours=rnd.uniform(0.5, SLA_HORAS[prioridad] * 1.5)), hoy)
            item['resuelto_en'] = iso(resuelto)
            actualizado = resuelto
            if rnd.random() < 0.03:  # algunos fueron escalados a problema
                item['escalado'] = True
                item['escalado_en'] = iso(resuelto)
            if estado == 'CERRADO':
                actualizado = min(resuelto + timedelta(hours=rnd.uniform(1, 48)), hoy)
                item['cerrado_en'] = iso(actualizado)
        item['actualizado_en'] = iso(actualizado)
        yield item


def sembrar(tenant):
    print(f'Sembrando {tenant} en {tabla.name} ...')
    with tabla.batch_writer(overwrite_by_pkeys=['tenant_id', 'incidente_id']) as lote:  # lotes de 25
        for i, item in enumerate(generar(tenant), start=1):
            lote.put_item(Item=item)
            if i % 1000 == 0:
                print(f'  {tenant}: {i}/{POR_TENANT}')
    # Contador en 10000: el primer incidente creado por la API sera inc-<tenant>-010001
    try:
        contadores.update_item(
            Key={'tenant_id': tenant, 'entidad': 'incidente'},
            UpdateExpression='SET valor = :v',
            ConditionExpression='attribute_not_exists(valor) OR valor < :v',
            ExpressionAttributeValues={':v': POR_TENANT},
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


if __name__ == '__main__':
    for t in TENANTS:
        sembrar(t)
    print('Listo.')
