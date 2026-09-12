"""Transaction-local EFMI adapter for an explicit blueprint object list."""
from contextlib import contextmanager
import inspect
import re


def component_id(obj):
    match = re.match(r'.*component[_ -]*(\d+).*', obj.name, re.IGNORECASE)
    if match is None:
        raise ValueError(f'Blueprint mesh has no Component N name: {obj.name}')
    return int(match.group(1))


@contextmanager
def explicit_efmi_objects(collection, objects):
    from velo_tools.games.arknights_endfield._efmi_core.blender_export import object_merger as core
    from velo_tools.games.arknights_endfield._efmi_core.blender_export.blender_export import ObjectMergerEFMI

    cls = ObjectMergerEFMI
    original = cls.import_objects_from_collection
    if list(inspect.signature(original).parameters) != ['self']:
        raise RuntimeError('Unsupported EFMI object collector signature')
    required = {'collection', 'component_id', 'force_object_name', 'allow_empty_components'}
    if not required.issubset(getattr(cls, '__dataclass_fields__', {})):
        raise RuntimeError('Unsupported EFMI object collector data model')
    selected = [(component_id(obj), obj) for obj in objects]
    owned = 'import_objects_from_collection' in cls.__dict__

    def collect(self):
        # LOD and unrelated exports retain their own collection handling.
        if self.collection != collection or self.force_object_name:
            return original(self)
        count = 0
        components = {component.id: component for component in self.components}
        for cid, obj in selected:
            if cid >= len(self.extracted_object.components):
                raise ValueError(f'Metadata is missing Component {cid}: {obj.name}')
            if self.component_id != -1 and cid != self.component_id:
                continue
            if cid not in components:
                raise RuntimeError(f'EFMI did not initialize Component {cid}')
            copied = core.copy_object(self.context, obj, name=f'TEMP_{obj.name}', collection=self.collection)
            components[cid].objects.append(core.TempObject(name=obj.name, object=copied))
            count += 1
        if not count and not self.allow_empty_components:
            raise ValueError('No blueprint meshes for this EFMI component')
        for component in self.components:
            component.objects.sort(key=lambda item: item.name)

    cls.import_objects_from_collection = collect
    try:
        yield
    finally:
        if owned:
            cls.import_objects_from_collection = original
        else:
            del cls.import_objects_from_collection
