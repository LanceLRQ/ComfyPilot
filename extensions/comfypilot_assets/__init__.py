import folder_paths

from server import PromptServer

from .bridge import create_routes

NODE_CLASS_MAPPINGS = {}

for route in create_routes(folder_paths.get_input_directory(), folder_paths.get_output_directory()):
    PromptServer.instance.routes.route(route.method, route.path, **route.kwargs)(route.handler)
