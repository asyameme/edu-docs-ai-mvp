from __future__ import annotations
from backend.contracts.document_v2.graph import GraphNode, GraphEdge, EducationalGraph
from backend.contracts.document_v2.models import parse_document


def _id(program_id: str | None, kind: str, key: str) -> str:
    return f'{program_id or "unassigned"}|{kind}|{key}'


def to_graph(document, program_id: str | None = None,) -> EducationalGraph:
    if isinstance(document, dict):
        document = parse_document(document)

    graph = EducationalGraph()
    meta = document.meta
    pid = meta.program_id

    if program_id is not None:
        if pid is not None and pid != program_id:
            raise ValueError(f"Program ID mismatch: {pid} != {program_id}")
        pid = program_id

    dtype = meta.doc_type

    def node(kind,key,ref,attrs=None):
        ident=_id(pid,kind,key)
        graph.add_node(GraphNode(id=ident,entity_type=kind,attributes=attrs or {},source_refs=[ref]))
        return ident
    def edge(src_kind,src_key,dst_kind,dst_key,rel,ref,semester=None,value=None):
        src=node(src_kind,src_key,ref)
        dst=node(dst_kind,dst_key,ref)
        graph.add_edge(GraphEdge(source_id=src,target_id=dst,relation_type=rel,source_ref=ref,document_type=dtype,semester=semester,value=value))
    program=getattr(document,'program',None)
    if program is not None:
        node('program',pid or meta.file_name,program.source_ref,program.model_dump(exclude={'source_ref'},exclude_none=True))
    for discipline in getattr(document,'disciplines',[]):
        node('discipline',discipline.name,discipline.source_ref,{'semesters':discipline.semesters,'credit_units':discipline.credit_units,'hours_total':discipline.hours_total})
    for competency in getattr(document,'competencies',[]):
        attrs = {}
        if competency.formulation is not None:
            attrs['formulation'] = competency.formulation
            node('competency', competency.code, competency.source_ref, attrs)
    for indicator in getattr(document,'indicators',[]):
        node('indicator',indicator.code,indicator.source_ref,{'text':indicator.text})
        edge('competency',indicator.competency_code,'indicator',indicator.code,'имеет',indicator.source_ref)
    for outcome in getattr(document,'learning_outcomes',[]):
        node('learning_outcome',f'{meta.file_name}::{outcome.local_id}',outcome.source_ref,{'text':outcome.text,'kind':outcome.kind,'code':outcome.code})
    for a in getattr(document,'assessment_tools',[]):
        node('assessment_tool',a.name,a.source_ref,{'description':getattr(a,'description',None),'task_examples':getattr(a,'task_examples',[])})
    for item in getattr(document,'curriculum_items',[]):
        node('curriculum_item',f'{item.discipline_name}::semester={item.semester}',item.source_ref,{'credit_units':item.credit_units,'hours_total':item.hours_total,'block':item.block})
    curriculum_keys = {}
    for item in getattr(document, 'curriculum_items', []):
        curriculum_keys.setdefault(item.discipline_name, []).append(f'{item.discipline_name}::semester={item.semester}')
    
    for r in getattr(document, 'relations', []):
        src_key = r.src_key
        dst_key = r.dst_key

        if r.src_entity == 'curriculum_item':
            matches = curriculum_keys.get(src_key)
            if matches:
                if len(matches) != 1:
                    raise ValueError(f'Неоднозначная запись учебного плана: {src_key}')
                src_key = matches[0]

        if r.dst_entity == 'curriculum_item':
            matches = curriculum_keys.get(dst_key)
            if matches:
                if len(matches) != 1:
                    raise ValueError(f'Неоднозначная запись учебного плана: {dst_key}')
                dst_key = matches[0]
        if r.src_entity=='learning_outcome': src_key=f'{meta.file_name}::{src_key}'
        if r.dst_entity=='learning_outcome': dst_key=f'{meta.file_name}::{dst_key}'
        edge(r.src_entity,src_key,r.dst_entity,dst_key,r.kind,r.source_ref)

    for cell in getattr(document,'matrix_cells',[]):
        if cell.value==1:
            edge('discipline',cell.discipline_name,'competency',cell.competency_code,'формирует',cell.source_ref,semester=cell.semester,value=1)
    for role in getattr(document,'professional_roles',[]):
        node('professional_role',f'{meta.file_name}::{role.local_id}',role.source_ref,{'name':role.name})
    for link in getattr(document,'role_competency_links',[]):
        edge('professional_role',f'{meta.file_name}::{link.role_id}','competency',link.competency_code,'связана_с_компетенцией',link.source_ref)
    for link in getattr(document,'indicator_outcome_links',[]):
        edge('indicator',link.indicator_code,'learning_outcome',f'{meta.file_name}::{link.learning_outcome_id}','описывается_результатом',link.source_ref)
    for workload in getattr(document,'semester_workload',[]):
        discipline=document.disciplines[0]
        wid=f'{discipline.name}::semester={workload.semester}'
        node('semester_workload',wid,workload.source_ref,{'hours_total':workload.hours_total,'credit_units':workload.credit_units})
        edge('discipline',discipline.name,'semester_workload',wid,'имеет_нагрузку',workload.source_ref,semester=workload.semester)

    
    # Сохраняем темы, виды занятий и часы из РПД/РПП.
    course_content = getattr(document, 'course_content', [])

    if course_content:
        disciplines = getattr(document, 'disciplines', [])

        if len(disciplines) != 1:
            raise ValueError('Нельзя однозначно связать course_content '
                'с дисциплиной: ожидается одна дисциплина')

        discipline = disciplines[0]

        for index, topic in enumerate(course_content):
            topic_key = f'{meta.file_name}::content_row={index}'

            node(
                'course_content',
                topic_key,
                topic.source_ref,
                {
                    'topic': topic.topic,
                    'activity': topic.activity,
                    'hours': topic.hours,
                    'module': topic.module,
                    'discipline_name': discipline.name,
                }
            )

            edge(
                'discipline',
                discipline.name,
                'course_content',
                topic_key,
                'содержит_тему',
                topic.source_ref
            )

    return graph


def merge_graphs(documents, program_id: str | None = None,) -> EducationalGraph:
    result = EducationalGraph()

    for doc in documents:
        graph = to_graph(doc, program_id=program_id)

        for node in graph.nodes:
            result.add_node(node)

        result.edges.extend(graph.edges)

    return result