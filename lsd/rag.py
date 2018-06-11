from networkx import Graph, connected_components
from scipy.ndimage.measurements import center_of_mass
import copy
import numpy as np
import skimage

class Rag(skimage.future.graph.RAG):
    '''A region adjacency graph (RAG) with the following attributes:

    Edge attributes:

        merged (int):

            Either 0 or 1, indicates whether an edge was selected for merging.

        agglomerated (int):

            Either 0 or 1, indicates whether an edge was processed by an
            agglomeration algorithm.

        center_{z,y,x} (float):

            A representative location of an edge. This is used (at least) by
            `class:SharedRagProvider<SharedRagProviders>` to query and write
            edges within a certain region of interest.

    Node attributes:

        labels (list of nodes):

            Stores for each node all nodes that were merged into this node.
            Agglomeration algorithms are expected to update this list as they
            modify the RAG (as scikit's ``merge_hierarchical`` does).

    Args:

        fragments (``ndarray``, optional):

            Creates a RAG from a label image. If not given, an empty RAG is
            created.

        connectivity (int, optional):

            The connectivity to consider for the RAG extraction from
            ``fragments``.
    '''

    def __init__(self, fragments=None, connectivity=2):

        super(Rag, self).__init__(fragments, connectivity)

        if fragments is not None:
            self.__find_edge_centers(fragments)
            self.__add_esential_edge_attributes()
            self.__add_esential_node_attributes()

    def set_edge_attributes(self, key, value):
        '''Set all the attribute of all edges to the given value.'''

        for _u, _v, data in self.edges_iter(data=True):
            data[key] = value

    def get_connected_components(self):
        '''Get all connected components in the RAG, as indicated by the
        'merged' attribute of edges.'''

        merge_graph = Graph()
        merge_graph.add_nodes_from(self.nodes())

        for u, v, data in self.edges_iter(data=True):
            if data['merged']:
                merge_graph.add_edge(u, v)

        components = connected_components(merge_graph)

        return [ list(component) for component in components ]

    def label_merged_edges(self, merged_rag):
        '''Set 'merged' to 1 for all edges that got merged in ``merged_rag``.

        ``merged_rag`` should be a RAG obtained from agglomeration a copy of
        this RAG, where each node has an attribute 'labels' that stores a list
        of the original nodes that make up the merged node.'''

        for merged_node, data in merged_rag.nodes_iter(data=True):
            for node in data['labels']:
                self.node[node]['merged_node'] = merged_node

        for u, v, data in self.edges_iter(data=True):
            if self.node[u]['merged_node'] == self.node[v]['merged_node']:
                data['merged'] = 1

    def contract_merged_nodes(self, fragments=None):
        '''Contract this RAG by merging all edges that have their 'merged'
        attribute set to 1.

        This will create new edges that will have only ``merged`` and
        ``agglomerated`` attributes, set to 0. Other edge attributes will be
        lost.

        Args:

            fragments (``ndarray``, optional):

                If given, also updates the labels in ``fragments`` according to
                the merges performed.
        '''

        # get currently connected componets
        components = self.get_connected_components()

        # replace each connected component by a single node
        component_nodes = self.__contract_nodes(components)

        if fragments is not None:

            # relabel fragments of the same connected components to match merged RAG
            self.__relabel(fragments, components, component_nodes)

    def copy(self):
        '''Return a deep copy of this RAG.'''

        return copy.deepcopy(self)

    def __find_edge_centers(self, fragments):
        '''Get the center of an edge as the mean of the fragment centroids.'''

        print(self.nodes())
        fragment_centers = {
            fragment: center
            for fragment, center in zip(
                self.nodes(),
                center_of_mass(fragments, fragments, self.nodes()))
        }

        for u, v, data in self.edges_iter(data=True):

            center_u = fragment_centers[u]
            center_v = fragment_centers[v]

            center_edge = tuple(
                (cu + cv)/2
                for cu, cv in zip(center_u, center_v)
            )

            data['center_z'] = center_edge[0]
            data['center_y'] = center_edge[1]
            data['center_x'] = center_edge[2]

    def __add_esential_edge_attributes(self):

        for u, v, data in self.edges_iter(data=True):

            if 'merged' not in data:
                data['merged'] = 0

            if 'agglomeration' not in data:
                data['agglomerated'] = 0

    def __add_esential_node_attributes(self):

        for node, data in self.nodes_iter(data=True):

            if 'labels' not in data:
                data['labels'] = [node]

    def __contract_nodes(self, components):
        '''Contract all nodes of one component into a single node, return the
        single node for each component.

        This will create new edges that will have only ``merged`` and
        ``agglomerated`` attributes, set to 0.
        '''

        self.__add_esential_node_attributes()

        component_nodes = []

        for component in components:

            for i in range(1, len(component)):
                self.merge_nodes(
                    component[i - 1],
                    component[i],
                    # set default attributes for new edges
                    weight_func=lambda _, _src, _dst, _n: {
                        'merged': 0,
                        'agglomerated': 0
                    })

            component_nodes.append(component[-1])

        return component_nodes

    def __relabel(self, array, components, component_labels):

        values_map = np.arange(int(array.max() + 1), dtype=array.dtype)

        for component, label in zip(components, component_labels):
            for c in component:
                if c < len(values_map):
                    values_map[c] = label

        array[:] = values_map[array]

