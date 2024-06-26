import torch
import numpy as np
from rfdiffusion.util import generate_Cbeta
import logging
import pandas as pd
from rfdiffusion.inference.utils import rigid_from_3_points

log = logging.getLogger(__name__)


class Potential:
    """
    Interface class that defines the functions a potential must implement
    """

    def compute(self, xyz, **kwargs):
        """
        Given the current structure of the model prediction, return the current
        potential as a PyTorch tensor with a single entry

        Args:
            xyz (torch.tensor, size: [L,27,3]: The current coordinates of the sample

        Returns:
            potential (torch.tensor, size: [1]): A potential whose value will be MAXIMIZED
                                                 by taking a step along it's gradient
        """
        raise NotImplementedError("Potential compute function was not overwritten")


class monomer_ROG(Potential):
    """
    Radius of Gyration potential for encouraging monomer compactness

    Written by DJ and refactored into a class by NRB
    """

    def __init__(self, weight=1, min_dist=15):
        self.weight = weight
        self.min_dist = min_dist

    def compute(self, xyz, **kwargs):
        Ca = xyz[:, 1]  # [L,3]

        centroid = torch.mean(Ca, dim=0, keepdim=True)  # [1,3]

        dgram = torch.cdist(
            Ca[None, ...].contiguous(), centroid[None, ...].contiguous(), p=2
        )  # [1,L,1,3]

        dgram = torch.maximum(
            self.min_dist * torch.ones_like(dgram.squeeze(0)), dgram.squeeze(0)
        )  # [L,1,3]

        rad_of_gyration = torch.sqrt(
            torch.sum(torch.square(dgram)) / Ca.shape[0]
        )  # [1]

        return -1 * self.weight * rad_of_gyration


class binder_ROG(Potential):
    """
    Radius of Gyration potential for encouraging binder compactness

    Author: NRB
    """

    def __init__(self, binderlen, weight=1, min_dist=15):
        self.binderlen = binderlen
        self.min_dist = min_dist
        self.weight = weight

    def compute(self, xyz, **kwargs):
        # Only look at binder residues
        Ca = xyz[: self.binderlen, 1]  # [Lb,3]

        centroid = torch.mean(Ca, dim=0, keepdim=True)  # [1,3]

        # cdist needs a batch dimension - NRB
        dgram = torch.cdist(
            Ca[None, ...].contiguous(), centroid[None, ...].contiguous(), p=2
        )  # [1,Lb,1,3]

        dgram = torch.maximum(
            self.min_dist * torch.ones_like(dgram.squeeze(0)), dgram.squeeze(0)
        )  # [Lb,1,3]

        rad_of_gyration = torch.sqrt(
            torch.sum(torch.square(dgram)) / Ca.shape[0]
        )  # [1]

        return -1 * self.weight * rad_of_gyration


class dimer_ROG(Potential):
    """
    Radius of Gyration potential for encouraging compactness of both monomers when designing dimers

    Author: PV
    """

    def __init__(self, binderlen, weight=1, min_dist=15):
        self.binderlen = binderlen
        self.min_dist = min_dist
        self.weight = weight

    def compute(self, xyz, **kwargs):
        # Only look at monomer 1 residues
        Ca_m1 = xyz[: self.binderlen, 1]  # [Lb,3]

        # Only look at monomer 2 residues
        Ca_m2 = xyz[self.binderlen :, 1]  # [Lb,3]

        centroid_m1 = torch.mean(Ca_m1, dim=0, keepdim=True)  # [1,3]
        centroid_m2 = torch.mean(Ca_m1, dim=0, keepdim=True)  # [1,3]

        # cdist needs a batch dimension - NRB
        # This calculates RoG for Monomer 1
        dgram_m1 = torch.cdist(
            Ca_m1[None, ...].contiguous(), centroid_m1[None, ...].contiguous(), p=2
        )  # [1,Lb,1,3]
        dgram_m1 = torch.maximum(
            self.min_dist * torch.ones_like(dgram_m1.squeeze(0)), dgram_m1.squeeze(0)
        )  # [Lb,1,3]
        rad_of_gyration_m1 = torch.sqrt(
            torch.sum(torch.square(dgram_m1)) / Ca_m1.shape[0]
        )  # [1]

        # cdist needs a batch dimension - NRB
        # This calculates RoG for Monomer 2
        dgram_m2 = torch.cdist(
            Ca_m2[None, ...].contiguous(), centroid_m2[None, ...].contiguous(), p=2
        )  # [1,Lb,1,3]
        dgram_m2 = torch.maximum(
            self.min_dist * torch.ones_like(dgram_m2.squeeze(0)), dgram_m2.squeeze(0)
        )  # [Lb,1,3]
        rad_of_gyration_m2 = torch.sqrt(
            torch.sum(torch.square(dgram_m2)) / Ca_m2.shape[0]
        )  # [1]

        # Potential value is the average of both radii of gyration (is avg. the best way to do this?)
        return -1 * self.weight * (rad_of_gyration_m1 + rad_of_gyration_m2) / 2


class binder_ncontacts(Potential):
    """
    Differentiable way to maximise number of contacts within a protein

    Motivation is given here: https://www.plumed.org/doc-v2.7/user-doc/html/_c_o_o_r_d_i_n_a_t_i_o_n.html

    """

    def __init__(self, binderlen, weight=1, r_0=8, d_0=4):
        self.binderlen = binderlen
        self.r_0 = r_0
        self.weight = weight
        self.d_0 = d_0

    def compute(self, xyz, **kwargs):
        # Only look at binder Ca residues
        Ca = xyz[: self.binderlen, 1]  # [Lb,3]

        # cdist needs a batch dimension - NRB
        dgram = torch.cdist(
            Ca[None, ...].contiguous(), Ca[None, ...].contiguous(), p=2
        )  # [1,Lb,Lb]
        divide_by_r_0 = (dgram - self.d_0) / self.r_0
        numerator = torch.pow(divide_by_r_0, 6)
        denominator = torch.pow(divide_by_r_0, 12)
        binder_ncontacts = (1 - numerator) / (1 - denominator)

        print("BINDER CONTACTS:", binder_ncontacts.sum())
        # Potential value is the average of both radii of gyration (is avg. the best way to do this?)
        return self.weight * binder_ncontacts.sum()


class interface_ncontacts(Potential):

    """
    Differentiable way to maximise number of contacts between binder and target

    Motivation is given here: https://www.plumed.org/doc-v2.7/user-doc/html/_c_o_o_r_d_i_n_a_t_i_o_n.html

    Author: PV
    """

    def __init__(self, binderlen, weight=1, r_0=8, d_0=6):
        self.binderlen = binderlen
        self.r_0 = r_0
        self.weight = weight
        self.d_0 = d_0

    def compute(self, xyz, **kwargs):
        # Extract binder Ca residues
        Ca_b = xyz[: self.binderlen, 1]  # [Lb,3]

        # Extract target Ca residues
        Ca_t = xyz[self.binderlen :, 1]  # [Lt,3]

        # cdist needs a batch dimension - NRB
        dgram = torch.cdist(
            Ca_b[None, ...].contiguous(), Ca_t[None, ...].contiguous(), p=2
        )  # [1,Lb,Lt]
        divide_by_r_0 = (dgram - self.d_0) / self.r_0
        numerator = torch.pow(divide_by_r_0, 6)
        denominator = torch.pow(divide_by_r_0, 12)
        interface_ncontacts = (1 - numerator) / (1 - denominator)
        # Potential is the sum of values in the tensor
        interface_ncontacts = interface_ncontacts.sum()

        print("INTERFACE CONTACTS:", interface_ncontacts.sum())

        return self.weight * interface_ncontacts


class monomer_contacts(Potential):
    """
    Differentiable way to maximise number of contacts within a protein

    Motivation is given here: https://www.plumed.org/doc-v2.7/user-doc/html/_c_o_o_r_d_i_n_a_t_i_o_n.html
    Author: PV

    NOTE: This function sometimes produces NaN's -- added check in reverse diffusion for nan grads
    """

    def __init__(self, weight=1, r_0=8, d_0=2, eps=1e-6):
        self.r_0 = r_0
        self.weight = weight
        self.d_0 = d_0
        self.eps = eps

    def compute(self, xyz, **kwargs):
        Ca = xyz[:, 1]  # [L,3]

        # cdist needs a batch dimension - NRB
        dgram = torch.cdist(
            Ca[None, ...].contiguous(), Ca[None, ...].contiguous(), p=2
        )  # [1,Lb,Lb]
        divide_by_r_0 = (dgram - self.d_0) / self.r_0
        numerator = torch.pow(divide_by_r_0, 6)
        denominator = torch.pow(divide_by_r_0, 12)

        ncontacts = (1 - numerator) / ((1 - denominator))

        # Potential value is the average of both radii of gyration (is avg. the best way to do this?)
        return self.weight * ncontacts.sum()


class olig_contacts(Potential):
    """
    Applies PV's num contacts potential within/between chains in symmetric oligomers

    Author: DJ
    """

    def __init__(
        self, contact_matrix, weight_intra=1, weight_inter=1, r_0=8, d_0=2, verbose=True
    ):
        """
        Parameters:
            chain_lengths (list, required): List of chain lengths, length is (Nchains)

            contact_matrix (torch.tensor/np.array, required):
                square matrix of shape (Nchains,Nchains) whose (i,j) enry represents
                attractive (1), repulsive (-1), or non-existent (0) contact potentials
                between chains in the complex

            weight (int/float, optional): Scaling/weighting factor
        """
        self.contact_matrix = contact_matrix
        self.weight_intra = weight_intra
        self.weight_inter = weight_inter
        self.r_0 = r_0
        self.d_0 = d_0
        self.verbose = verbose

        # check contact matrix only contains valid entries
        assert all(
            [i in [-1, 0, 1] for i in contact_matrix.flatten()]
        ), "Contact matrix must contain only 0, 1, or -1 in entries"
        # assert the matrix is square and symmetric
        shape = contact_matrix.shape
        assert len(shape) == 2
        assert shape[0] == shape[1]
        for i in range(shape[0]):
            for j in range(shape[1]):
                assert contact_matrix[i, j] == contact_matrix[j, i]
        self.nchain = shape[0]

    def _get_idx(self, i, L):
        """
        Returns the zero-indexed indices of the residues in chain i
        """
        assert L % self.nchain == 0
        Lchain = L // self.nchain
        return i * Lchain + torch.arange(Lchain)

    def compute(self, xyz, **kwargs):
        """
        Iterate through the contact matrix, compute contact potentials between chains that need it,
        and negate contacts for any
        """
        L = xyz.shape[0]

        intra_contacts, inter_contacts = 0, 0
        start = 0
        for i in range(self.nchain):
            for j in range(self.nchain):
                # only compute for upper triangle, disregard zeros in contact matrix
                if (i <= j) and (self.contact_matrix[i, j] != 0):
                    # get the indices for these two chains
                    idx_i = self._get_idx(i, L)
                    idx_j = self._get_idx(j, L)

                    Ca_i = xyz[idx_i, 1]  # slice out crds for this chain
                    Ca_j = xyz[idx_j, 1]  # slice out crds for that chain
                    dgram = torch.cdist(
                        Ca_i[None, ...].contiguous(), Ca_j[None, ...].contiguous(), p=2
                    )  # [1,Lb,Lb]

                    divide_by_r_0 = (dgram - self.d_0) / self.r_0
                    numerator = torch.pow(divide_by_r_0, 6)
                    denominator = torch.pow(divide_by_r_0, 12)
                    ncontacts = (1 - numerator) / (1 - denominator)

                    if i == j:
                        # weight, don't double count intra
                        intra_contacts += (
                            ncontacts.sum() * self.contact_matrix[i, j] / 2
                        )
                    else:
                        inter_contacts += ncontacts.sum() * self.contact_matrix[i, j]

        all_contacts = (
            self.weight_intra * intra_contacts + self.weight_inter * inter_contacts
        )
        if self.verbose:
            log.info(
                f"'olig_contacts' guiding potential: "
                f"intra_contacts={intra_contacts:.3g}, "
                f"inter_contacts={inter_contacts:.3g}, "
                f"potential={all_contacts:.3g}"
            )
        return all_contacts


def get_damped_lj(r_min, r_lin, p1=6, p2=12):
    y_at_r_lin = lj(r_lin, r_min, p1, p2)
    ydot_at_r_lin = lj_grad(r_lin, r_min, p1, p2)

    def inner(dgram):
        return (dgram < r_lin) * (ydot_at_r_lin * (dgram - r_lin) + y_at_r_lin) + (
            dgram >= r_lin
        ) * lj(dgram, r_min, p1, p2)

    return inner


def lj(dgram, r_min, p1=6, p2=12):
    return 4 * (
        (r_min / (2 ** (1 / p1) * dgram)) ** p2
        - (r_min / (2 ** (1 / p1) * dgram)) ** p1
    )


def lj_grad(dgram, r_min, p1=6, p2=12):
    return -p2 * r_min**p1 * (r_min**p1 - dgram**p1) / (dgram ** (p2 + 1))


def mask_expand(mask, n=1):
    mask_out = mask.clone()
    assert mask.ndim == 1
    for i in torch.where(mask)[0]:
        for j in range(i - n, i + n + 1):
            if j >= 0 and j < len(mask):
                mask_out[j] = True
    return mask_out


def contact_energy(dgram, d_0, r_0):
    divide_by_r_0 = (dgram - d_0) / r_0
    numerator = torch.pow(divide_by_r_0, 6)
    denominator = torch.pow(divide_by_r_0, 12)

    ncontacts = (1 - numerator) / ((1 - denominator)).float()
    return -ncontacts


def poly_repulse(dgram, r, slope, p=1):
    a = slope / (p * r ** (p - 1))

    return (dgram < r) * a * torch.abs(r - dgram) ** p * slope


# def only_top_n(dgram


class substrate_contacts(Potential):
    """
    Implicitly models a ligand with an attractive-repulsive potential.
    """

    def __init__(
        self, weight=1, r_0=8, d_0=2, s=1, eps=1e-6, rep_r_0=5, rep_s=2, rep_r_min=1
    ):
        self.r_0 = r_0
        self.weight = weight
        self.d_0 = d_0
        self.eps = eps

        # motif frame coordinates
        # NOTE: these probably need to be set after sample_init() call, because the motif sequence position in design must be known
        self.motif_frame = None  # [4,3] xyz coordinates from 4 atoms of input motif
        self.motif_mapping = None  # list of tuples giving positions of above atoms in design [(resi, atom_idx)]
        self.motif_substrate_atoms = (
            None  # xyz coordinates of substrate from input motif
        )
        r_min = 2
        self.energies = []
        self.energies.append(
            lambda dgram: s * contact_energy(torch.min(dgram, dim=-1)[0], d_0, r_0)
        )
        if rep_r_min:
            self.energies.append(
                lambda dgram: poly_repulse(
                    torch.min(dgram, dim=-1)[0], rep_r_0, rep_s, p=1.5
                )
            )
        else:
            self.energies.append(
                lambda dgram: poly_repulse(dgram, rep_r_0, rep_s, p=1.5)
            )

    def compute(self, xyz, **kwargs):
        # First, get random set of atoms
        # This operates on self.xyz_motif, which is assigned to this class in the model runner (for horrible plumbing reasons)
        self._grab_motif_residues(self.xyz_motif)

        # for checking affine transformation is corect
        first_distance = torch.sqrt(
            torch.sqrt(
                torch.sum(
                    torch.square(self.motif_substrate_atoms[0] - self.motif_frame[0]),
                    dim=-1,
                )
            )
        )

        # grab the coordinates of the corresponding atoms in the new frame using mapping
        res = torch.tensor([k[0] for k in self.motif_mapping])
        atoms = torch.tensor([k[1] for k in self.motif_mapping])
        new_frame = xyz[self.diffusion_mask][res, atoms, :]
        # calculate affine transformation matrix and translation vector b/w new frame and motif frame
        A, t = self._recover_affine(self.motif_frame, new_frame)
        # apply affine transformation to substrate atoms
        substrate_atoms = (
            torch.mm(A, self.motif_substrate_atoms.transpose(0, 1)).transpose(0, 1) + t
        )
        second_distance = torch.sqrt(
            torch.sqrt(
                torch.sum(torch.square(new_frame[0] - substrate_atoms[0]), dim=-1)
            )
        )
        assert abs(first_distance - second_distance) < 0.01, "Alignment seems to be bad"
        diffusion_mask = mask_expand(self.diffusion_mask, 1)
        Ca = xyz[~diffusion_mask, 1]

        # cdist needs a batch dimension - NRB
        dgram = torch.cdist(
            Ca[None, ...].contiguous(), substrate_atoms.float()[None], p=2
        )[
            0
        ]  # [Lb,Lb]

        all_energies = []
        for i, energy_fn in enumerate(self.energies):
            energy = energy_fn(dgram)
            all_energies.append(energy.sum())
        return -self.weight * sum(all_energies)

        # Potential value is the average of both radii of gyration (is avg. the best way to do this?)
        return self.weight * ncontacts.sum()

    def _recover_affine(self, frame1, frame2):
        """
        Uses Simplex Affine Matrix (SAM) formula to recover affine transform between two sets of 4 xyz coordinates
        See: https://www.researchgate.net/publication/332410209_Beginner%27s_guide_to_mapping_simplexes_affinely

        Args:
        frame1 - 4 coordinates from starting frame [4,3]
        frame2 - 4 coordinates from ending frame [4,3]

        Outputs:
        A - affine transformation matrix from frame1->frame2
        t - affine translation vector from frame1->frame2
        """

        l = len(frame1)
        # construct SAM denominator matrix
        B = torch.vstack([frame1.T, torch.ones(l)])
        D = 1.0 / torch.linalg.det(B)  # SAM denominator

        M = torch.zeros((3, 4), dtype=torch.float64)
        for i, R in enumerate(frame2.T):
            for j in range(l):
                num = torch.vstack([R, B])
                # make SAM numerator matrix
                num = torch.cat((num[: j + 1], num[j + 2 :]))  # make numerator matrix
                # calculate SAM entry
                M[i][j] = (-1) ** j * D * torch.linalg.det(num)

        A, t = torch.hsplit(M, [l - 1])
        t = t.transpose(0, 1)
        return A, t

    def _grab_motif_residues(self, xyz) -> None:
        """
        Grabs 4 atoms in the motif.
        Currently random subset of Ca atoms if the motif is >= 4 residues, or else 4 random atoms from a single residue
        """
        idx = torch.arange(self.diffusion_mask.shape[0])
        idx = idx[self.diffusion_mask].float()
        if torch.sum(self.diffusion_mask) >= 4:
            rand_idx = torch.multinomial(idx, 4).long()
            # get Ca atoms
            self.motif_frame = xyz[rand_idx, 1]
            self.motif_mapping = [(i, 1) for i in rand_idx]
        else:
            rand_idx = torch.multinomial(idx, 1).long()
            self.motif_frame = xyz[rand_idx[0], :4]
            self.motif_mapping = [(rand_idx, i) for i in range(4)]


def get_z_profile(coords, z=None, steps=40):
    if z is None:
        z = torch.linspace(coords[:, 2].min() - 0.1, coords[:, 2].max() + 0.1, steps)
    # bucketize coords according to their z values.
    # bin_idxs can have at most z.shape[0]+1 unique numbers
    bin_idxs = torch.bucketize(coords[:, 2], boundaries=z, right=True)
    r = (coords[:, :2] * coords[:, :2]).sum(1).sqrt()
    rmin = torch.zeros(z.shape[0] + 1).scatter_reduce(
        dim=0, src=r, index=bin_idxs, reduce="amin", include_self=False
    )
    rmean = torch.zeros(z.shape[0] + 1).scatter_reduce(
        dim=0, src=r, index=bin_idxs, reduce="mean", include_self=False
    )
    rmax = torch.zeros(z.shape[0] + 1).scatter_reduce(
        dim=0, src=r, index=bin_idxs, reduce="amax", include_self=False
    )
    return torch.stack([z, rmin[:-1], rmean[:-1], rmax[:-1]], dim=1)


class z_profile(Potential):
    """
    Applies inner/outer z-profile constraint in symmetric oligomers.
    """

    def __init__(
        self, contact_matrix, profile_csv, cutoff=None, weight=1, verbose=True
    ):
        """
        Parameters:

            contact_matrix (torch.tensor/np.array, required):
                This is a dummy argument kept only for consistency

            profile_csv (path):
                path to the csv file containing profile data. The file should have 4 columns {z, rmin, rmean, rmax}

            cutoff (float):
                if passed, only deviations from the the target profile which are larger than `cutoff` are penalised.

            weight (int/float, optional): Scaling/weighting factor

            verbose (bool):
                if True, informative messages are added to log.
        """
        self.contact_matrix = contact_matrix
        self.weight = weight
        self.target_profile = self.read_profile(profile_csv)
        self.cutoff = cutoff
        self.verbose = verbose

    @staticmethod
    def read_profile(csv):
        return torch.from_numpy(pd.read_csv(csv).values).float()

    def get_z_profile(self, coords, z=None, steps=100):
        return get_z_profile(coords, z=z, steps=steps)

    def compute(self, xyz, **kwargs):
        coords = xyz[:, 1].contiguous()
        current_profile = self.get_z_profile(coords, z=self.target_profile[:, 0])
        idx = current_profile[:, 1] > 0
        deviations = (current_profile[idx, 1:] - self.target_profile[idx, 1:]) ** 2
        if self.cutoff:
            deviations = deviations[deviations > self.cutoff**2]
        pot = -deviations.sum()
        if self.verbose:
            log.info(f"'z_profile' guiding potential: " f"potential={pot.item():.2g}")
        return self.weight * pot


def get_Rgs(coords, diagonalise=False):
    N = coords.shape[0]
    diff = coords - coords.mean(0, keepdim=True)
    cov = torch.matmul(diff.T, diff) / N
    if diagonalise:
        return torch.linalg.eigvalsh(cov).sqrt()
    return cov.diag().sqrt()


class Rgs(Potential):
    """
    Applies a guiding potential to bias Rgx, Rgy and Rgz values.
    """

    def __init__(
        self,
        contact_matrix,
        Rgx=None,
        Rgy=None,
        Rgz=None,
        diagonalise=False,
        weight=1,
        verbose=True,
    ):
        """
        Parameters:

            contact_matrix (torch.tensor/np.array, required):
                This is a dummy argument kept only for consistency

            Rg{x,y,z} (float):
                Target values for the 3 radii of gyration in x, y and z direction. If diagonalise is True,
                The gyration radii are sorted with Rgx being the smallest.

            diagonalise (bool):
                If True, the gyration tensor is diagonalised and the sorted radii of gyration corresponding to
                the main axis of the gyration tensor are used instead.

            weight (int/float, optional): Scaling/weighting factor

            verbose (bool):
                if True, informative messages are added to log.

        Notes:
            If any of the Rg{x,y,z} arguments are not set (i.e. if it has the default None value), not guiding
            potential is imposed for that component. At least one of {Rgx, Rgy, Rgz} values must be given.

        """
        self.contact_matrix = contact_matrix
        self.weight = weight
        self.diagonalise = diagonalise
        self.target_Rgs = [Rgx, Rgy, Rgz]
        assert self.target_Rgs is not [
            None,
            None,
            None,
        ], "At least one of {Rgx, Rgy, Rgz} values must be given!"
        self.verbose = verbose

    def get_Rgs(self, coords):
        return get_Rgs(coords, self.diagonalise)

    def compute(self, xyz, **kwargs):
        Rgs = self.get_Rgs(xyz[:, 1].contiguous())
        pot = -sum(
            [(x - y) ** 2 for x, y in zip(Rgs, self.target_Rgs) if y is not None]
        )
        if self.verbose:
            log.info(
                f"'Rgs' guiding potential: "
                f"current_Rgs={[float(f'{x:.2f}') for x in Rgs.tolist()]}, "
                f"target_Rgs={[float(f'{x:.2f}') if x is not None else x for x in self.target_Rgs]}, "
                f"potential={pot.item():.2g}"
            )
        return self.weight * pot


def add_ideal_oxygen(xyz, non_ideal=False):
    """
    Adds an ideal backbone Oxygen if the corresponding coordinates in xyz has nan.
    """
    xyz_in = xyz.clone().float()
    if xyz.ndim == 3:
        xyz_in = xyz_in.unsqueeze(0)

    if xyz_in.shape[2] == 3:
        # only N,Ca,C are given
        xyz_in = torch.cat(
            [xyz_in, torch.full_like(xyz_in[..., 0:1, :], float("nan"))], dim=2
        )

    mask = xyz_in[0, :, 3, :].isnan().any(-1)
    if mask.sum() == 0:
        return xyz

    log.info(mask)
    log.info(xyz_in[0, 0, :4])

    Rs, Ts = rigid_from_3_points(
        xyz_in[:, mask, 0, :],
        xyz_in[:, mask, 1, :],
        xyz_in[:, mask, 2, :],
        non_ideal=non_ideal,
    )

    # ideal atom positions are listed in
    # https://github.com/mosayebi/RFdiffusion/blob/main/rfdiffusion/chemical.py#L196
    log.info("adding ideal oxygen(s)")
    Oideal = torch.tensor([0.6303, 1.0574, 0.000], device=xyz_in.device)
    xyz_in[:, mask, 3, :] = torch.einsum("brij,j->bri", Rs, Oideal) + Ts
    return xyz_in.squeeze(0) if xyz.ndim == 3 else xyz_in


def get_bb_hbond_map(coords):
    """
    Parameters:
        coords (torch.Tensor): (N, CA, C, O) coordinates with shape (num_residues, 4, 3)
    Returns:
        Returns backbone hbond map tensor using pydssp. shape is (num_residues, num_residues)
        for more details see https://github.com/ShintaroMinami/PyDSSP
    """
    try:
        import pydssp
    except:
        raise RuntimeError(
            f"'pydssp' cannot be imported! see https://github.com/ShintaroMinami/PyDSSP"
        )
    coords = add_ideal_oxygen(coords)
    return pydssp.get_hbond_map(coords)


class hb_contacts(Potential):
    """
    Applies a potential to maximise number of hydrogen bonds as found by pydssp
    """

    def __init__(
        self, contact_matrix, weight_intra=1, weight_inter=1, mode=0, verbose=True
    ):
        """
        Parameters:
            contact_matrix (torch.tensor/np.array, required):
                square matrix of shape (Nchains,Nchains) whose (i,j) enry represents
                attractive (1), repulsive (-1), or non-existent (0) contact potentials
                between chains in the complex

            weight_intra (int/float, optional):
                Scaling/weighting factor for intra Hbonds

            weight_inter (int/float, optional):
                Scaling/weighting factor for inter Hbonds

            verbose (bool):
                if True, informative messages are added to log.
        """
        self.contact_matrix = contact_matrix
        self.weight_intra = weight_intra
        self.weight_inter = weight_inter
        self.verbose = verbose
        self.mode = mode

        # check contact matrix only contains valid entries
        assert all(
            [i in [-1, 0, 1] for i in contact_matrix.flatten()]
        ), "Contact matrix must contain only 0, 1, or -1 in entries"
        # assert the matrix is square and symmetric
        shape = contact_matrix.shape
        assert len(shape) == 2
        assert shape[0] == shape[1]
        for i in range(shape[0]):
            for j in range(shape[1]):
                assert contact_matrix[i, j] == contact_matrix[j, i]
        self.nchain = shape[0]

    def _get_idx(self, i, L):
        """
        Returns the zero-indexed indices of the residues in chain i
        """
        assert L % self.nchain == 0
        Lchain = L // self.nchain
        return i * Lchain + torch.arange(Lchain)

    def _sum(self, tensor):
        if self.mode == 0:
            return tensor.sum()
        elif self.mode == 1:
            return (tensor[tensor > 0]).sum()

    def compute(self, xyz, **kwargs):
        """
        Iterate through the contact matrix, compute contact potentials between chains that need it,
        and negate contacts for any
        """
        L = xyz.shape[0]

        hb_intra = sum(
            [
                self._sum(get_bb_hbond_map(xyz[self._get_idx(i, L), :4].contiguous()))
                for i in range(self.nchain)
            ]
        )

        hb_inter = sum(
            [
                self._sum(
                    get_bb_hbond_map(
                        torch.cat(
                            [
                                xyz[self._get_idx(i, L), :4].contiguous(),
                                xyz[self._get_idx(j, L), :4].contiguous(),
                            ],
                            dim=0,
                        )
                    )
                )
                for i in range(self.nchain)
                for j in range(i + 1, self.nchain)
                if i != j
            ]
        )

        all_hb = self.weight_intra * hb_intra + self.weight_inter * hb_inter
        if self.verbose:
            log.info(
                f"'hb_contacts' guiding potential: mode={self.mode}, "
                f"intra_hb_contacts={hb_intra:.3g}, "
                f"inter_hb_contacts={hb_inter:.3g}, "
                f"potential={all_hb:.3g}"
            )
        return all_hb


from rfdiffusion.chemical import aa2long, num2aa


def prep_madrax_input(xyz, seq_in):
    # take glycines, except for motif region
    if seq_in.ndim>1:
        seq = torch.where(
            torch.argmax(seq_in, dim=-1) == 21, 7, torch.argmax(seq_in, dim=-1)
        )  # 7 is glycine
    else:
        seq = torch.where(seq_in == 21, 7, seq_in)  # 7 is glycine

    idx_pdb = 1 + torch.arange(xyz.shape[0])
    chain = "A"
    natoms = xyz.shape[1]
    assert natoms == 14 or natoms == 27
    coords = []
    atnames = []
    ctr = 1
    for i, s in enumerate(seq.squeeze()):
        atms = aa2long[s]
        # his prot hack
        if s == 8 and torch.linalg.norm(xyz[i, 9, :] - xyz[i, 5, :]) < 1.7:
            atms = (
                " N  ",
                " CA ",
                " C  ",
                " O  ",
                " CB ",
                " CG ",
                " NE2",
                " CD2",
                " CE1",
                " ND1",
                None,
                None,
                None,
                None,
                " H  ",
                " HA ",
                "1HB ",
                "2HB ",
                " HD2",
                " HE1",
                " HD1",
                None,
                None,
                None,
                None,
                None,
                None,
            )  # his_d
        for j, atm_j in enumerate(atms):
            if j < natoms and atm_j is not None:
                atnames.append(f"{num2aa[s]}_{ctr}_{atm_j.replace(' ','')}_{chain}_0_0")
                coords.append(xyz[i, j, :])
        ctr += 1
    atnames = [atnames]
    coords = torch.stack(coords).unsqueeze(0)
    return coords, atnames


def get_madrax_energy(xyz, seq_in, FF=None, device="cpu", clip=None):
    from madrax import dataStructures

    if device is None:
        device = xyz.device
    if FF is None:
        from madrax.ForceField import ForceField  # the main MadraX module
        FF = ForceField(device=device)
    else:
        FF.to(device)
    coords, atnames = prep_madrax_input(xyz, seq_in)
    info_tensors = dataStructures.create_info_tensors(atnames, device=device)
    E = FF(coords.to(device), info_tensors)
    if clip:
        E.clamp_(min=-clip, max=clip)
    return E


class madrax_energy(Potential):
    """
    Applies a potential to minimise the madrax energy
    see https://www.biorxiv.org/content/10.1101/2023.01.12.523724v1
    """

    def __init__(
        self,
        contact_matrix,
        weight_intra=1,
        weight_inter=1,
        clip=None,
        verbose=True,
        device="cuda",
    ):
        """
        Parameters:
            contact_matrix (torch.tensor/np.array, required):
                square matrix of shape (Nchains,Nchains) whose (i,j) enry represents
                attractive (1), repulsive (-1), or non-existent (0) contact potentials
                between chains in the complex

            weight_intra (int/float, optional):
                Scaling/weighting factor for intra Hbonds

            weight_inter (int/float, optional):
                Scaling/weighting factor for inter Hbonds

            verbose (bool):
                if True, informative messages are added to log.
        """
        from madrax.ForceField import ForceField

        self.contact_matrix = contact_matrix
        self.weight_intra = weight_intra
        self.weight_inter = weight_inter
        self.clip = clip
        self.device = device
        self.verbose = verbose
        self.FF = ForceField(self.device)

        # check contact matrix only contains valid entries
        assert all(
            [i in [-1, 0, 1] for i in contact_matrix.flatten()]
        ), "Contact matrix must contain only 0, 1, or -1 in entries"
        # assert the matrix is square and symmetric
        shape = contact_matrix.shape
        assert len(shape) == 2
        assert shape[0] == shape[1]
        for i in range(shape[0]):
            for j in range(shape[1]):
                assert contact_matrix[i, j] == contact_matrix[j, i]
        self.nchain = shape[0]

    def _get_idx(self, i, L):
        """
        Returns the zero-indexed indices of the residues in chain i
        """
        assert L % self.nchain == 0
        Lchain = L // self.nchain
        return i * Lchain + torch.arange(Lchain)

    def compute(self, xyz, **kwargs):
        """
        Iterate through the contact matrix, compute contact potentials between chains that need it,
        and negate contacts for any
        """
        L = xyz.shape[0]
        seq_in = kwargs["seq_in"]
        e_intra = sum(
            [
                get_madrax_energy(
                    xyz[self._get_idx(i, L)].contiguous(),
                    seq_in[self._get_idx(i, L)].contiguous(),
                    FF=self.FF,
                    clip=self.clip,
                    device=self.device,
                ).sum()
                for i in range(self.nchain)
            ]
        )
        e_inter = sum(
            [
                get_madrax_energy(
                    torch.cat(
                        [
                            xyz[self._get_idx(i, L)].contiguous(),
                            xyz[self._get_idx(j, L)].contiguous(),
                        ],
                        dim=0,
                    ),
                    torch.cat(
                        [
                            seq_in[self._get_idx(i, L)].contiguous(),
                            seq_in[self._get_idx(j, L)].contiguous(),
                        ],
                        dim=0,
                    ),
                    FF=self.FF,
                    clip=self.clip,
                    device=self.device,
                ).sum()
                for i in range(self.nchain)
                for j in range(i + 1, self.nchain)
                if i != j
            ]
        )

        all_e = -(self.weight_intra * e_intra + self.weight_inter * e_inter)
        if self.verbose:
            log.info(
                f"'madrax_energy' guiding potential: clip_value={self.clip}, "
                f"intra_energy={e_intra:.3g}, "
                f"inter_energy={e_inter:.3g}, "
                f"potential={all_e:.3g}"
            )
        return all_e


# Dictionary of types of potentials indexed by name of potential. Used by PotentialManager.
# If you implement a new potential you must add it to this dictionary for it to be used by
# the PotentialManager
implemented_potentials = {
    "monomer_ROG": monomer_ROG,
    "binder_ROG": binder_ROG,
    "dimer_ROG": dimer_ROG,
    "binder_ncontacts": binder_ncontacts,
    "interface_ncontacts": interface_ncontacts,
    "monomer_contacts": monomer_contacts,
    "olig_contacts": olig_contacts,
    "substrate_contacts": substrate_contacts,
    "z_profile": z_profile,
    "Rgs": Rgs,
    "hb_contacts": hb_contacts,
    "madrax_energy": madrax_energy,
}

require_binderlen = {
    "binder_ROG",
    "binder_distance_ReLU",
    "binder_any_ReLU",
    "dimer_ROG",
    "binder_ncontacts",
    "interface_ncontacts",
}
