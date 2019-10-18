import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import torch.distributions as dist

from schnetpack.nn.base import Dense
from schnetpack import Properties
from schnetpack.nn.cfconv import CFConv
from schnetpack.nn.cutoff import HardCutoff
from schnetpack.nn.acsf import GaussianSmearing
from schnetpack.nn.neighbors import AtomDistances
from schnetpack.nn.activations import shifted_softplus


class SchNetInteraction(nn.Module):
    r"""SchNet interaction block for modeling interactions of atomistic systems.

    Args:
        n_atom_basis (int): number of features to describe atomic environments.
        n_spatial_basis (int): number of input features of filter-generating networks.
        n_filters (int): number of filters used in continuous-filter convolution.
        cutoff (float): cutoff radius.
        cutoff_network (nn.Module, optional): cutoff layer.
        normalize_filter (bool, optional): if True, divide aggregated filter by number
            of neighbors over which convolution is applied.

    """

    def __init__(
        self,
        n_atom_basis,
        n_spatial_basis,
        n_filters,
        cutoff,
        cutoff_network=HardCutoff,
        normalize_filter=False,
    ):
        super(SchNetInteraction, self).__init__()
        # filter block used in interaction block
        self.filter_network = nn.Sequential(
            Dense(n_spatial_basis, n_filters, activation=shifted_softplus),
            Dense(n_filters, n_filters),
        )
        # cutoff layer used in interaction block
        self.cutoff_network = cutoff_network(cutoff)
        # interaction block
        self.cfconv = CFConv(
            n_atom_basis,
            n_filters,
            n_atom_basis,
            self.filter_network,
            cutoff_network=self.cutoff_network,
            activation=shifted_softplus,
            normalize_filter=normalize_filter,
        )
        # dense layer
        self.dense = Dense(n_atom_basis, n_atom_basis, bias=True, activation=None)

    def forward(self, x, r_ij, neighbors, neighbor_mask, f_ij=None):
        """Compute interaction output.

        Args:
            x (torch.Tensor): input representation/embedding of atomic environments
                with (N_b, N_a, n_atom_basis) shape.
            r_ij (torch.Tensor): interatomic distances of (N_b, N_a, N_nbh) shape.
            neighbors (torch.Tensor): indices of neighbors of (N_b, N_a, N_nbh) shape.
            neighbor_mask (torch.Tensor): mask to filter out non-existing neighbors
                introduced via padding.
            f_ij (torch.Tensor, optional): expanded interatomic distances in a basis.
                If None, r_ij.unsqueeze(-1) is used.

        Returns:
            torch.Tensor: block output with (N_b, N_a, n_atom_basis) shape.

        """
        # continuous-filter convolution interaction block followed by Dense layer
        v = self.cfconv(x, r_ij, neighbors, neighbor_mask, f_ij)
        v = self.dense(v)
        return v


class SchNet(nn.Module):
    """SchNet architecture for learning representations of atomistic systems.

    Args:
        n_atom_basis (int, optional): number of features to describe atomic environments.
            This determines the size of each embedding vector; i.e. embeddings_dim.
        n_filters (int, optional): number of filters used in continuous-filter convolution
        n_interactions (int, optional): number of interaction blocks.
        cutoff (float, optional): cutoff radius.
        n_gaussians (int, optional): number of Gaussian functions used to expand
            atomic distances.
        normalize_filter (bool, optional): if True, divide aggregated filter by number
            of neighbors over which convolution is applied.
        coupled_interactions (bool, optional): if True, share the weights across
            interaction blocks and filter-generating networks.
        return_intermediate (bool, optional): if True, `forward` method also returns
            intermediate atomic representations after each interaction block is applied.
        max_z (int, optional): maximum nuclear charge allowed in database. This
            determines the size of the dictionary of embedding; i.e. num_embeddings.
        cutoff_network (nn.Module, optional): cutoff layer.
        trainable_gaussians (bool, optional): If True, widths and offset of Gaussian
            functions are adjusted during training process.
        distance_expansion (nn.Module, optional): layer for expanding interatomic
            distances in a basis.
        charged_systems (bool, optional):

    References:
    .. [#schnet1] Schütt, Arbabzadah, Chmiela, Müller, Tkatchenko:
       Quantum-chemical insights from deep tensor neural networks.
       Nature Communications, 8, 13890. 2017.
    .. [#schnet_transfer] Schütt, Kindermans, Sauceda, Chmiela, Tkatchenko, Müller:
       SchNet: A continuous-filter convolutional neural network for modeling quantum
       interactions.
       In Advances in Neural Information Processing Systems, pp. 992-1002. 2017.
    .. [#schnet3] Schütt, Sauceda, Kindermans, Tkatchenko, Müller:
       SchNet - a deep learning architecture for molceules and materials.
       The Journal of Chemical Physics 148 (24), 241722. 2018.

    """

    def __init__(
        self,
        n_atom_basis=128,
        n_filters=128,
        n_interactions=3,
        cutoff=5.0,
        n_gaussians=25,
        normalize_filter=False,
        coupled_interactions=False,
        return_intermediate=False,
        max_z=100,
        cutoff_network=HardCutoff,
        trainable_gaussians=False,
        distance_expansion=None,
        charged_systems=False,
        use_noise=False,
        noise_mean=0,
        noise_std=1,
        chargeEmbedding = True,
        selectFet = None,
    ):
        super(SchNet, self).__init__()

        self.chargeEmbedding = chargeEmbedding
        self.n_atom_basis = n_atom_basis
        self.selectFet = selectFet

        # make a lookup table to store embeddings for each element (up to atomic
        # number max_z) each of which is a vector of size n_atom_basis
        if self.chargeEmbedding and selectFet is None:
            self.embedding = nn.Embedding(max_z, int(n_atom_basis / 2), padding_idx=0)
            self.dense = Dense(8, int(n_atom_basis / 2))
            #print('A')
        elif not self.chargeEmbedding and selectFet is None:
            self.dense = Dense(8, n_atom_basis)
            #print('B')
        elif self.chargeEmbedding and selectFet is not None:
            self.embedding = nn.Embedding(max_z, int(n_atom_basis / 2), padding_idx=0)
            self.dense = Dense(1, int(n_atom_basis / 2))
        else:
            self.dense = Dense(1, n_atom_basis)
            #print('D')

        # layer for computing interatomic distances
        self.distances = AtomDistances()

        # layer for expanding interatomic distances in a basis
        if distance_expansion is None:
            self.distance_expansion = GaussianSmearing(
                0.0, cutoff, n_gaussians, trainable=trainable_gaussians
            )
        else:
            self.distance_expansion = distance_expansion

        # block for computing interaction
        if isinstance(n_filters, list):
            self.interactions = nn.ModuleList(
                [
                    SchNetInteraction(
                        n_atom_basis=n_atom_basis,
                        n_spatial_basis=n_gaussians,
                        n_filters=n_filters[i],
                        cutoff_network=cutoff_network,
                        cutoff=cutoff,
                        normalize_filter=normalize_filter,
                    )
                    for i in range(n_interactions)
                ]
            )

        elif coupled_interactions:
            # use the same SchNetInteraction instance (hence the same weights)
            self.interactions = nn.ModuleList(
                [
                    SchNetInteraction(
                        n_atom_basis=n_atom_basis,
                        n_spatial_basis=n_gaussians,
                        n_filters=n_filters,
                        cutoff_network=cutoff_network,
                        cutoff=cutoff,
                        normalize_filter=normalize_filter,
                    )
                ]
                * n_interactions
            )
        else:
            # use one SchNetInteraction instance for each interaction
            self.interactions = nn.ModuleList(
                [
                    SchNetInteraction(
                        n_atom_basis=n_atom_basis,
                        n_spatial_basis=n_gaussians,
                        n_filters=n_filters,
                        cutoff_network=cutoff_network,
                        cutoff=cutoff,
                        normalize_filter=normalize_filter,
                    )
                    for _ in range(n_interactions)
                ]
            )

        # set attributes
        self.use_noise = use_noise
        self.noise_mean = noise_mean
        self.noise_std = noise_std
        self.return_intermediate = return_intermediate
        self.charged_systems = charged_systems
        if charged_systems:
            self.charge = nn.Parameter(torch.Tensor(1, n_atom_basis))
            self.charge.data.normal_(0, 1.0 / n_atom_basis ** 0.5)
        self.newpos = Variable(torch.rand([1,600,3])).cuda()

    def forward(self, inputs):
        """Compute atomic representations/embeddings.

        Args:
            inputs (dict of torch.Tensor): SchNetPack dictionary of input tensors.

        Returns:
            torch.Tensor: atom-wise representation.
            list of torch.Tensor: intermediate atom-wise representations, if
            return_intermediate=True was used.

        """
        # get tensors from input dictionary
        atomic_numbers = inputs[Properties.Z]
        positions = self.newpos #inputs[Properties.R]
        cell = inputs[Properties.cell]
        cell_offset = inputs[Properties.cell_offset]
        neighbors = inputs[Properties.neighbors]
        neighbor_mask = inputs[Properties.neighbor_mask]
        atom_mask = inputs[Properties.atom_mask]
        ligand_indicator = inputs['props'][:,:,7:8]
        print('li: ', ligand_indicator.size())

        if self.selectFet is not None:
            props = inputs['props'][:,:,self.selectFet:self.selectFet + 1]
            for i in range(len(props)):
                yes = False
                for k in range(len(props[0])):
                    if props[i][k][0] == 1:
                        print('prot', i)
                        yes = True
                    else:
                        print('lig')
                if not yes:
                    print(i, ' has no prot')
            print(props)
        else:
            props = inputs['props']
        '''
        for i in range(len(props)):
            for k in props[i]:
                print(k)
                if k[7]:
                    print('hi')
        '''
        #print(p.size())

        if self.chargeEmbedding:
        # get atom embeddings for the input atomic numbers
            #print(props.size())
            p = self.dense(props)
            x = self.embedding(atomic_numbers)

            x = torch.cat((p,x), 2)
            print(x.size())
        else:
            #print(props.size())
            x = self.dense(props)


        if False and self.charged_systems and Properties.charge in inputs.keys():
            n_atoms = torch.sum(atom_mask, dim=1, keepdim=True)
            charge = inputs[Properties.charge] / n_atoms  # B
            charge = charge[:, None] * self.charge  # B x F
            x = x + charge

        shape = positions.size()
        print(positions)
        noise = Variable(torch.rand(shape) * self.noise_mean).cuda()
        if self.use_noise:
            positions # + noise

        # compute interatomic distance of every atom to its neighbors
        r_ij = self.distances(
            positions, neighbors, cell, cell_offset, neighbor_mask=neighbor_mask
        )
        # expand interatomic distances (for example, Gaussian smearing)
        f_ij = self.distance_expansion(r_ij)
        # store intermediate representations
        if self.return_intermediate:
            xs = [x]
        # compute interaction block to update atomic embeddings
        for interaction in self.interactions:
            v = interaction(x, r_ij, neighbors, neighbor_mask, f_ij=f_ij)
            x = x + v
            if self.return_intermediate:
                xs.append(x)
        #x = torch.cat((x, ligand_indicator), 2)
        if self.return_intermediate:
            return x, xs
        return x
