import torch
import torch.nn.functional as F
import numpy as np
from tqdm.auto import tqdm
import math
import random
import copy
from torch.utils.data import DataLoader
from sklearn.metrics.pairwise import pairwise_distances

class SelectionStrategy:
    """
    Base class for selection strategies.
    """
    
    def __init__(self, name, model, device=None):
        """
        Initialize selection strategy.
        
        Args:
            name: Name of the strategy
            model: Model to use for predictions
            device: Device to use for computations
        """
        self.name = name
        self.model = model
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")


class ExampleSelectionStrategy(SelectionStrategy):
    """
    Base class for Active Learning strategies that select which examples to annotate.
    """
    
    def select_examples(self, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Select examples for annotation.
        
        Args:
            dataset: Dataset to select from
            num_to_select: Number of examples to select
            costs: Dictionary mapping example indices to their annotation costs
            **kwargs: Additional arguments specific to the strategy
            
        Returns:
            list: Indices of selected examples
            list: Corresponding scores for selected examples
        """
        raise NotImplementedError("Subclasses must implement select_examples method")


class FeatureSelectionStrategy(SelectionStrategy):
    """
    Base class for Active Feature Acquisition strategies that select which 
    features (positions) to annotate within an example.
    """
    
    def select_features(self, example_idx, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Select features (positions) to annotate within a given example.
        
        Args:
            example_idx: Index of the example to select features from
            dataset: Dataset containing the example
            num_to_select: Number of features to select
            costs: Dictionary mapping positions to their annotation costs
            **kwargs: Additional arguments specific to the strategy
            
        Returns:
            list: Tuples of (position_idx, benefit, cost, benefit/cost_ratio) for selected positions
        """
        raise NotImplementedError("Subclasses must implement select_features method")
    
    def select_batch_features(self, example_indices, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Select features for multiple examples.
        
        Args:
            example_indices: Indices of examples to select features from
            dataset: Dataset containing the examples
            num_to_select: Number of features to select per example
            costs: Dictionary mapping (example_idx, position_idx) to costs
            **kwargs: Additional arguments specific to the strategy
            
        Returns:
            dict: Mapping from example indices to selected position indices with scores
        """
        selections = {}
        for idx in example_indices:
            example_costs = None
            if costs and idx in costs:
                example_costs = costs[idx]
                
            selected_positions = self.select_features(
                idx, dataset, num_to_select, costs=example_costs, **kwargs
            )
            selections[idx] = selected_positions
        return selections


class RandomExampleSelectionStrategy(ExampleSelectionStrategy):
    """
    Random example selection strategy for Active Learning.
    
    This strategy randomly selects examples without considering model predictions.
    It establishes a baseline for more sophisticated strategies.
    """
    
    def __init__(self, model, device=None):
        """Initialize random example selection strategy."""
        super().__init__("random_example", model, device)
    
    def select_examples(self, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Randomly select examples for annotation.
        
        Args:
            dataset: Dataset to select from
            num_to_select: Number of examples to select
            costs: Dictionary mapping example indices to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            list: Indices of selected examples
            list: Scores (set to 1.0) for selected examples
        """
        # Get all valid examples (with at least one masked position)
        valid_indices = []
        
        for idx in range(len(dataset)):
            masked_positions = dataset.get_masked_positions(idx)
            if masked_positions:
                valid_indices.append(idx)
        
        # Select random indices
        if len(valid_indices) <= num_to_select:
            selected_indices = valid_indices
        else:
            selected_indices = random.sample(valid_indices, num_to_select)
        
        # Assign uniform scores of 1.0 (no real scoring for random)
        scores = [1.0] * len(selected_indices)
        
        return selected_indices, scores


class RandomFeatureSelectionStrategy(FeatureSelectionStrategy):
    """
    Random feature selection strategy for Active Feature Acquisition.
    
    This strategy randomly selects features within an example without
    considering model predictions. It establishes a baseline for more
    sophisticated feature selection strategies.
    """
    
    def __init__(self, model, device=None):
        """Initialize random feature selection strategy."""
        super().__init__("random_feature", model, device)
    
    def select_features(self, example_idx, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Randomly select features (positions) to annotate within a given example.
        
        Args:
            example_idx: Index of the example to select features from
            dataset: Dataset containing the example
            num_to_select: Number of features to select
            costs: Dictionary mapping positions to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            list: Tuples of (position_idx, benefit, cost, benefit/cost_ratio) for selected positions
        """
        # Get all masked positions for this example
        masked_positions = dataset.get_masked_positions(example_idx)
        
        # Select random positions
        if len(masked_positions) <= num_to_select:
            selected_positions = masked_positions
        else:
            selected_positions = random.sample(masked_positions, num_to_select)
        
        # Construct result with benefit/cost information
        result = []
        for pos in selected_positions:
            # Default cost is 1.0 if not specified
            cost = 1.0
            if costs and pos in costs:
                cost = costs[pos]
                
            # For random selection, benefit equals cost (benefit/cost ratio = 1.0)
            benefit = cost
            ratio = 1.0
            
            result.append((pos, benefit, cost, ratio))
        
        return result


class VOICalculator:
    """
    Value of Information (VOI) calculator.
    
    Computes the expected reduction in loss (benefit) from observing a variable,
    considering the cost of observation for true benefit/cost analysis.
    """
    
    def __init__(self, model, device=None):
        """
        Initialize VOI calculator.
        
        Args:
            model: Model to use for predictions
            device: Device to use for computations
        """
        self.model = model
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    def compute_loss(self, pred, loss_type="cross_entropy"):
        """
        Compute loss for prediction.
        
        Args:
            pred: Prediction logits
            loss_type: Type of loss to compute ("cross_entropy", "l2", or "0-1")
            
        Returns:
            float: Loss value
        """
        if loss_type == "cross_entropy" or loss_type == "nll":
            # Entropy of the distribution (uncertainty)
            probs = F.softmax(pred, dim=-1)
            return -torch.sum(probs * torch.log(probs + 1e-10), dim=-1).mean().item()
            
        elif loss_type == "l2":
            # Variance of the predicted distribution
            probs = F.softmax(pred, dim=-1)
            scores = torch.arange(1, 6, device=self.device).float()
            mean = torch.sum(probs * scores, dim=-1)
            variance = torch.sum(probs * (scores - mean.unsqueeze(-1)) ** 2, dim=-1).mean().item()
            return variance 
            
        elif loss_type == "0-1":
            # 1 - maximum probability (uncertainty in classification)
            probs = F.softmax(pred, dim=-1)
            max_prob = probs.max(dim=-1)[0].mean().item()
            return 1 - max_prob 
    
    def compute_voi(self, model, inputs, annotators, questions, embeddings, known_questions, candidate_idx, 
                   target_indices, loss_type="cross_entropy", cost=1.0):
        """
        Compute VOI using batch processing approach.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor [batch_size, sequence_length, input_dim]
            annotators: Annotator indices [batch_size, sequence_length]
            questions: Question indices [batch_size, sequence_length]
            known_questions: Mask indicating known questions [batch_size, sequence_length]
            candidate_idx: Index of candidate annotation to evaluate
            target_indices: Target indices to compute loss on
            loss_type: Type of loss to compute
            cost: Cost of annotating this position
            
        Returns:
            tuple: (voi_value, voi/cost_ratio, expected_posterior_loss)
        """
        model.eval()

        with torch.no_grad():
            # Get initial outputs and compute initial loss
            outputs = model(inputs, annotators, questions, embeddings)
            
            # Extract predictions for target indices
            if isinstance(target_indices, list) and len(target_indices) == 1:
                target_idx = target_indices[0]
                target_preds = outputs[:, target_idx, :]
            else:
                # Handle multiple target indices - concatenate predictions
                target_preds = torch.cat([outputs[:, idx, :].unsqueeze(1) for idx in target_indices], dim=1)
            
            # Compute initial loss
            loss_initial = self.compute_loss(target_preds, loss_type)
            
            # Get prediction for candidate
            candidate_pred = outputs[:, candidate_idx, :]
            candidate_probs = F.softmax(candidate_pred, dim=-1)
            
            batch_size = inputs.shape[0]
            num_classes = candidate_probs.shape[-1]  # Usually 5 for our case
            
            # Process all possible answers in one batch
            expanded_inputs = []
            expanded_annotators = []
            expanded_questions = []
            expanded_embeddings = []
            
            for i in range(num_classes):
                # Create a copy of inputs with candidate set to class i
                input_with_answer = inputs.clone()
                one_hot = F.one_hot(torch.tensor(i), num_classes=num_classes).float().to(self.device)
                input_with_answer[:, candidate_idx, -num_classes:] = one_hot
                input_with_answer[:, candidate_idx, 0] = 0  # Mark as observed
                
                expanded_inputs.append(input_with_answer)
                expanded_annotators.append(annotators.clone())
                expanded_questions.append(questions.clone())
                expanded_embeddings.append(embeddings.clone())
            
            expanded_inputs = torch.cat(expanded_inputs, dim=0)
            expanded_annotators = torch.cat(expanded_annotators, dim=0)
            expanded_questions = torch.cat(expanded_questions, dim=0)
            expanded_embeddings = torch.cat(expanded_embeddings, dim=0)
            
            # Get predictions for all possible answers
            expanded_outputs = model(expanded_inputs, expanded_annotators, expanded_questions, expanded_embeddings)
            
            # Extract target predictions for each possible answer
            all_losses = []
            
            for i in range(num_classes):
                class_batch_start = i * batch_size
                class_batch_end = (i + 1) * batch_size
                
                if isinstance(target_indices, list) and len(target_indices) == 1:
                    target_idx = target_indices[0]
                    class_target_preds = expanded_outputs[class_batch_start:class_batch_end, target_idx, :]
                else:
                    # Handle multiple target indices
                    class_target_preds = torch.cat([
                        expanded_outputs[class_batch_start:class_batch_end, idx, :].unsqueeze(1) 
                        for idx in target_indices
                    ], dim=1)
                
                # Compute loss for this class assignment
                class_loss = self.compute_loss(class_target_preds, loss_type)
                all_losses.append(class_loss)
            
            # Weight losses by candidate distribution
            expected_posterior_loss = 0.0
            for i in range(num_classes):
                expected_posterior_loss += candidate_probs[0, i].item() * all_losses[i]
            
            # VOI is the expected reduction in loss
            voi = loss_initial - expected_posterior_loss
            
            # Compute benefit/cost ratio
            voi_cost_ratio = voi / max(cost, 1e-10)
            
            return voi, voi_cost_ratio, expected_posterior_loss


class FastVOICalculator(VOICalculator):
    """
    Fast VOI calculator that uses gradient-based approximation for efficiency.
    
    Approximates VOI by calculating gradients of query outputs with respect to inputs,
    making VOI computation much faster for large problems.
    """
    
    def __init__(self, model, device=None, loss_type="cross_entropy"):
        """Initialize Fast VOI calculator."""
        super().__init__(model, device)
        self.loss_type = loss_type
    
    def compute_fast_voi(self, model, inputs, annotators, questions, known_questions, embeddings, candidate_idx, target_indices, loss_type=None, num_samples=3, cost=1.0):
        """
        Compute VOI using gradient-based approximation.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor [batch_size, sequence_length, input_dim]
            annotators: Annotator indices [batch_size, sequence_length]
            questions: Question indices [batch_size, sequence_length]
            known_questions: Mask indicating known questions [batch_size, sequence_length]
            candidate_idx: Index of candidate annotation to evaluate
            target_indices: Target indices to compute loss on
            loss_type: Type of loss to compute ("cross_entropy", "l2", or "0-1")
            num_samples: Number of samples to use for approximation
            cost: Cost of annotating this position
            
        Returns:
            tuple: (voi_value, voi/cost_ratio, expected_posterior_loss, most_informative_class)
        """
        model.eval()
        loss_type = loss_type or self.loss_type
        batch_size = inputs.shape[0]
        input_dim = inputs.shape[2]
        
        # Enable gradients temporarily for approximation
        with torch.enable_grad():
            inputs_grad = inputs.clone().requires_grad_(True)
            
            # Forward pass
            outputs = model(inputs_grad, annotators, questions, embeddings)
            
            # Extract predictions for target indices
            if isinstance(target_indices, list) and len(target_indices) == 1:
                target_idx = target_indices[0]
                target_preds = outputs[:, target_idx, :]
            else:
                # Handle multiple target indices - concatenate predictions
                target_preds = torch.cat([outputs[:, idx, :].unsqueeze(1) for idx in target_indices], dim=1)
            
            # Get candidate variable distribution
            candidate_pred = outputs[:, candidate_idx, :]
            candidate_probs = F.softmax(candidate_pred, dim=-1)
            num_classes = candidate_probs.shape[-1]
            
            # Compute initial loss
            initial_loss = self.compute_loss(target_preds, loss_type)
            
            # For each target dimension, compute gradient with respect to candidate input
            # This tells us how changing candidate affects target predictions
            target_gradients = []
            
            for dim in range(target_preds.shape[-1]):
                # For each dimension of the target prediction
                grad = torch.autograd.grad(
                    outputs=target_preds[0, dim],
                    inputs=inputs_grad,
                    grad_outputs=torch.ones_like(target_preds[0, dim]),
                    retain_graph=True
                )[0]

                grad_slice = grad[0, candidate_idx, 1:1+num_classes]
                target_gradients.append(grad_slice)
            
            # Stack gradients to form a matrix of shape [target_dims, candidate_dims]
            gradient_matrix = torch.stack(target_gradients)
            
            # Approximate effect of each possible value of candidate variable
            expected_posterior_loss = 0.0
            class_losses = []
            
            for class_idx in range(num_classes):
                # Create one-hot distribution for this class
                one_hot = torch.zeros(num_classes, device=self.device)
                one_hot[class_idx] = 1.0
                
                # Calculate change in distribution
                delta_prob = one_hot - candidate_probs[0]
                
                effects = torch.matmul(gradient_matrix, delta_prob)
                
                # Apply effect to get approximate new target predictions
                approx_target_preds = target_preds.clone()
                for dim in range(target_preds.shape[-1]):
                    approx_target_preds[0, dim] += effects[dim]
                
                # Compute loss with this approximation
                class_loss = self.compute_loss(approx_target_preds, loss_type)
                class_losses.append(class_loss)
                
                # Weight by probability of this class
                expected_posterior_loss += candidate_probs[0, class_idx].item() * class_loss
        
        # VOI is the expected reduction in loss
        voi = initial_loss - expected_posterior_loss
        
        # Find most informative class (highest individual VOI)
        class_vois = [initial_loss - loss for loss in class_losses]
        most_informative_class = int(np.argmax(class_vois))
        
        # Compute benefit/cost ratio
        voi_cost_ratio = voi / max(cost, 1e-10)
        
        return voi, voi_cost_ratio, expected_posterior_loss, most_informative_class
    
    def compute_loss(self, pred, loss_type="cross_entropy"):
        """
        Compute loss for prediction.
        
        Args:
            pred: Prediction logits
            loss_type: Type of loss to compute ("cross_entropy", "l2", or "0-1")
            
        Returns:
            float: Loss value
        """
        if loss_type == "cross_entropy" or loss_type == "nll":
            # Entropy of the distribution (uncertainty)
            probs = F.softmax(pred, dim=-1)
            return -torch.sum(probs * torch.log(probs + 1e-10), dim=-1).mean().item()
            
        elif loss_type == "l2":
            # L2 loss (squared error) between expected rating and possible true ratings
            probs = F.softmax(pred, dim=-1)
            scores = torch.arange(1, 6, device=self.device).float()
            
            # Calculate expected rating
            expected_rating = torch.sum(probs * scores, dim=-1)
            
            # Calculate squared errors for each possible true rating
            squared_errors = torch.zeros_like(probs)
            for i in range(scores.shape[0]):
                true_rating = scores[i]
                squared_errors[:, i] = (expected_rating - true_rating) ** 2
            
            # Expected squared error
            return torch.sum(probs * squared_errors, dim=-1).mean().item()
            
        elif loss_type == "0-1":
            # 1 - maximum probability (uncertainty in classification)
            probs = F.softmax(pred, dim=-1)
            max_prob = probs.max(dim=-1)[0].mean().item()
            return 1 - max_prob

class VOISelectionStrategy(FeatureSelectionStrategy):
    """
    VOI-based feature selection strategy for Active Feature Acquisition.
    
    Selects features that provide the highest value of information (expected
    reduction in loss) per unit cost, making annotation more cost-effective.
    """
    
    def __init__(self, model, device=None):
        """Initialize VOI selection strategy."""
        super().__init__("voi", model, device)
        self.voi_calculator = VOICalculator(model, device)

    def select_features(self, example_idx, dataset, num_to_select=1, target_questions=None, 
                   loss_type="cross_entropy", costs=None, **kwargs):
        
        if target_questions is None:
            target_questions = [0]
        
        if isinstance(target_questions[0], str):
            question_list = ['Q0', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6']
            target_questions = [question_list.index(q) for q in target_questions if q in question_list]
        
        masked_positions = dataset.get_masked_positions(example_idx)
        if not masked_positions:
            return []
        
        known_questions, inputs, answers, annotators, questions, embeddings = dataset[example_idx]
        inputs = inputs.unsqueeze(0).to(self.device)
        answers = answers.unsqueeze(0).to(self.device)
        annotators = annotators.unsqueeze(0).to(self.device)
        questions = questions.unsqueeze(0).to(self.device)
        known_questions = known_questions.unsqueeze(0).to(self.device)
        if embeddings is not None:
            embeddings = embeddings.unsqueeze(0).to(self.device)
        
        target_indices = []
        for q_idx in target_questions:
            for i in range(questions.shape[1]):
                if (questions[0, i].item() == q_idx and 
                    not dataset.is_position_noisy(example_idx, i)):
                    target_indices.append(i)
        
        if not target_indices:
            return []
        
        position_vois = []
        for position in masked_positions:
            cost = 1.0
            if costs and position in costs:
                cost = costs[position]
                
            voi, voi_cost_ratio, posterior_loss = self.voi_calculator.compute_voi(
                self.model, inputs, annotators, questions, embeddings, known_questions,
                position, target_indices, loss_type, cost=cost
            )
            
            position_vois.append((position, voi, cost, voi_cost_ratio))
        
        position_vois.sort(key=lambda x: x[3], reverse=True)
        
        return position_vois[:num_to_select]
    
    # def select_features(self, example_idx, dataset, num_to_select=1, target_questions=None, 
    #                    loss_type="cross_entropy", costs=None, **kwargs):
    #     """
    #     Select features (positions) using VOI within a given example.
        
    #     Args:
    #         example_idx: Index of the example to select features from
    #         dataset: Dataset containing the example
    #         num_to_select: Number of features to select
    #         target_questions: Target questions to compute VOI for
    #         loss_type: Type of loss to compute
    #         costs: Dictionary mapping positions to their annotation costs
    #         **kwargs: Additional arguments
            
    #     Returns:
    #         list: Tuples of (position_idx, benefit, cost, benefit/cost_ratio) for selected positions
    #     """
    #     if target_questions is None:
    #         # Default to first question (Q0)
    #         target_questions = [0]
        
    #     # Convert target questions to indices if needed
    #     if isinstance(target_questions[0], str):
    #         question_list = ['Q0', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6']
    #         target_questions = [question_list.index(q) for q in target_questions if q in question_list]
        
    #     # Get masked positions
    #     masked_positions = dataset.get_masked_positions(example_idx)
    #     if not masked_positions:
    #         return []
        
    #     # Get data
    #     known_questions, inputs, answers, annotators, questions, embeddings = dataset[example_idx]
    #     inputs = inputs.unsqueeze(0).to(self.device)
    #     answers = answers.unsqueeze(0).to(self.device)
    #     annotators = annotators.unsqueeze(0).to(self.device)
    #     questions = questions.unsqueeze(0).to(self.device)
    #     known_questions = known_questions.unsqueeze(0).to(self.device)
    #     if embeddings is not None:
    #         embeddings = embeddings.unsqueeze(0).to(self.device)
        
    #     # Find target indices (positions that have the target questions)
    #     # target_indices = []
    #     # for q_idx in target_questions:
    #     #     for i in range(questions.shape[1]):
    #     #         if questions[0, i].item() == q_idx and annotators[0, i].item() >= 0:  # Human annotation
    #     #             target_indices.append(i)

    #     target_indices = []
    #     for q_idx in target_questions:
    #         for i in range(questions.shape[1]):
    #             if (questions[0, i].item() == q_idx and 
    #                 annotators[0, i].item() >= 0 and  # Human annotation (not LLM)
    #                 not dataset.is_position_noisy(example_idx, i)):  # NOT noisy
    #                 target_indices.append(i)
    #                 break  # Take only first original human target per question
        
    #     if not target_indices:
    #         return []
        
    #     # Calculate VOI for each masked position
    #     position_vois = []
    #     for position in masked_positions:
    #         # Get cost for this position
    #         cost = 1.0  # Default cost
    #         if costs and position in costs:
    #             cost = costs[position]
                
    #         # Compute VOI
    #         voi, voi_cost_ratio, posterior_loss = self.voi_calculator.compute_voi(
    #             self.model, inputs, annotators, questions, embeddings, known_questions,
    #             position, target_indices, loss_type, cost=cost
    #         )
            
    #         position_vois.append((position, voi, cost, voi_cost_ratio))
        
    #     # Sort by benefit/cost ratio (highest first)
    #     position_vois.sort(key=lambda x: x[3], reverse=True)
        
    #     # Return top selections
    #     return position_vois[:num_to_select]

class FastVOISelectionStrategy(FeatureSelectionStrategy):
    """
    Fast VOI-based feature selection strategy for Active Feature Acquisition.
    
    Uses gradient-based approximation to estimate VOI, making it 
    computationally efficient while providing accurate selections.
    """
    
    def __init__(self, model, device=None, loss_type="cross_entropy"):
        """Initialize Fast VOI selection strategy."""
        super().__init__("fast_voi", model, device)
        self.voi_calculator = FastVOICalculator(model, device, loss_type)
        self.loss_type = loss_type
    
    def select_features(self, example_idx, dataset, num_to_select=1, target_questions=None, 
                       loss_type=None, num_samples=3, costs=None, **kwargs):
        """
        Select features (positions) using gradient-based VOI approximation.
        
        Args:
            example_idx: Index of the example to select features from
            dataset: Dataset containing the example
            num_to_select: Number of features to select
            target_questions: Target questions to compute VOI for
            loss_type: Type of loss to compute ("cross_entropy", "l2")
            num_samples: Number of samples to use for approximation
            costs: Dictionary mapping positions to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            list: Tuples of (position_idx, benefit, cost, benefit/cost_ratio, class_idx) for selected positions
        """
        loss_type = loss_type or self.loss_type
        
        if target_questions is None:
            # Default to first question (Q0)
            target_questions = [0]
        
        # Convert target questions to indices if needed
        if isinstance(target_questions[0], str):
            question_list = ['Q0', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6']
            target_questions = [question_list.index(q) for q in target_questions if q in question_list]
        
        # Get masked positions
        masked_positions = dataset.get_masked_positions(example_idx)
        if not masked_positions:
            return []
        
        # Get data
        known_questions, inputs, answers, annotators, questions, embeddings = dataset[example_idx]
        inputs = inputs.unsqueeze(0).to(self.device)
        answers = answers.unsqueeze(0).to(self.device)
        annotators = annotators.unsqueeze(0).to(self.device)
        questions = questions.unsqueeze(0).to(self.device)
        known_questions = known_questions.unsqueeze(0).to(self.device)
        if embeddings is not None:
            embeddings = embeddings.unsqueeze(0).to(self.device)
        
        # Find target indices (positions that have the target questions)
        target_indices = []
        for q_idx in target_questions:
            for i in range(questions.shape[1]):
                if questions[0, i].item() == q_idx and annotators[0, i].item() >= 0:  # Human annotation
                    target_indices.append(i)
        
        if not target_indices:
            return []
        
        # Calculate Fast VOI for each masked position
        position_vois = []
        for position in masked_positions:
            # Get cost for this position
            cost = 1.0  # Default cost
            if costs and position in costs:
                cost = costs[position]
                
            # Compute Fast VOI
            voi, voi_cost_ratio, posterior_loss, most_informative_class = self.voi_calculator.compute_fast_voi(
                self.model, inputs, annotators, questions, known_questions, embeddings,
                position, target_indices, loss_type, num_samples, cost=cost
            )
            
            position_vois.append((position, voi, cost, voi_cost_ratio, most_informative_class))
        
        # Sort by benefit/cost ratio (highest first)
        position_vois.sort(key=lambda x: x[3], reverse=True)
        
        # Return top selections
        return position_vois[:num_to_select]

class GradientSelector:
    """
    Helper class for gradient-based selection.
    
    Computes and compares gradients for active learning,
    selecting examples that would provide the most training benefit.
    """
    
    def __init__(self, model, device=None):
        """
        Initialize gradient selector.
        
        Args:
            model: Model to use for predictions
            device: Device to use for computations
        """
        self.model = model
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    def normalize_gradient(self, grad_dict):
        """
        Normalize gradients by their total L2 norm.
        
        Args:
            grad_dict: Dictionary of gradients
            
        Returns:
            dict: Normalized gradients
        """
        total_norm_squared = 0.0
        for name, grad in grad_dict.items():
            total_norm_squared += torch.sum(grad ** 2).item()
        
        if total_norm_squared <= 1e-10:
            return grad_dict
        
        total_norm = math.sqrt(total_norm_squared)
        normalized_grad_dict = {}
        
        for name, grad in grad_dict.items():
            normalized_grad_dict[name] = grad / total_norm
        
        return normalized_grad_dict
    
    def compute_grad_dot_product(self, grad_dict1, grad_dict2):
        """
        Compute dot product between two gradient dictionaries.
        
        Args:
            grad_dict1: First gradient dictionary
            grad_dict2: Second gradient dictionary
            
        Returns:
            float: Dot product
        """
        dot_product = 0.0
        
        for name in grad_dict1:
            if name in grad_dict2:
                dot_product += torch.sum(-grad_dict1[name] * grad_dict2[name]).item()
        
        return dot_product
    
    def compute_sample_gradient(self, model, inputs, labels, annotators, questions, embeddings):
        """
        Compute gradient for a single example using autoregressive sampling.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor
            labels: Label tensor
            annotators: Annotator indices
            questions: Question indices
            
        Returns:
            dict: Gradient dictionary
        """
        model.train()
        grad_dict = {}
        
        # Identify masked positions
        masked_positions = []
        for j in range(inputs.shape[1]):
            if inputs[0, j, 0] == 1:
                masked_positions.append(j)
        
        if not masked_positions:
            for name, param in model.named_parameters():
                if param.requires_grad:
                    grad_dict[name] = torch.zeros_like(param)
            return grad_dict
        
        temp_inputs = inputs.clone()
        temp_labels = labels.clone()
        
        for pos in masked_positions:
            with torch.no_grad():
                current_outputs = model(temp_inputs, annotators, questions, embeddings)
                var_outputs = current_outputs[0, pos]
                var_probs = F.softmax(var_outputs, dim=0)
            
            sampled_class = torch.multinomial(var_probs, 1).item()
            
            one_hot = torch.zeros(model.max_choices, device=self.device)
            one_hot[sampled_class] = 1.0
            
            temp_inputs[0, pos, 0] = 0
            temp_inputs[0, pos, 1:1+model.max_choices] = one_hot
            
            temp_labels[0, pos] = one_hot
        
        # Compute loss with full supervision
        model.zero_grad()
        
        outputs = model(temp_inputs, annotators, questions, embeddings)
        loss = model.compute_total_loss(
            outputs, temp_labels, temp_inputs, questions, embeddings,
            full_supervision=True
        )
        
        # Compute gradients
        loss.backward()
        
        # Collect gradients
        for name, param in model.named_parameters():
            if param.requires_grad and param.grad is not None:
                grad_dict[name] = param.grad.detach().clone()
        
        model.zero_grad()
        
        return grad_dict
    
    def compute_example_gradients(self, model, inputs, labels, annotators, questions, embeddings, num_samples=5):
        """
        Compute gradients for a single example with multiple samples.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor
            labels: Label tensor
            annotators: Annotator indices
            questions: Question indices
            num_samples: Number of samples to compute
            
        Returns:
            dict: Gradient dictionary
        """
        grad_dict = {}
        
        for _ in range(num_samples):
            sample_grad_dict = self.compute_sample_gradient(
                model, inputs, labels, annotators, questions, embeddings
            )
            
            # Accumulate gradients
            for name, grad in sample_grad_dict.items():
                if name not in grad_dict:
                    grad_dict[name] = grad
                else:
                    grad_dict[name] += grad
        
        # Average over samples
        if num_samples > 0:
            for name in grad_dict:
                grad_dict[name] /= num_samples
        
        return grad_dict
    
    def compute_validation_gradient_sampled(self, model, val_dataloader, num_samples=5):
        """
        Compute validation gradients using sampling approach.
        
        Args:
            model: Model to use for predictions
            val_dataloader: Validation dataloader
            num_samples: Number of samples to compute
            
        Returns:
            list: List of gradient dictionaries
        """
        model.train()
        grad_samples = []
        
        for _ in tqdm(range(num_samples), desc="Computing validation gradients"):
            temp_grad_dict = {}
            sample_count = 0
            
            for batch in val_dataloader:
                known_questions, inputs, labels, annotators, questions, embeddings = batch
                inputs, labels, annotators, questions = (
                    inputs.to(self.device), labels.to(self.device), 
                    annotators.to(self.device), questions.to(self.device)
                )

                if embeddings is not None:
                    embeddings = embeddings.unsqueeze(0).to(self.device)
                
                batch_size = inputs.shape[0]
                
                temp_inputs = inputs.clone()
                
                for i in range(batch_size):
                    masked_positions = []
                    for j in range(inputs.shape[1]):
                        if temp_inputs[i, j, 0] == 1:
                            masked_positions.append(j)
                    
                    # Sample values for masked positions
                    for pos in masked_positions:
                        with torch.no_grad():
                            current_outputs = model(temp_inputs, annotators, questions, embeddings)
                            var_outputs = current_outputs[i, pos]
                            var_probs = F.softmax(var_outputs, dim=0)
                        
                        # Sample a class
                        sampled_class = torch.multinomial(var_probs, 1).item()
                        
                        # Create one-hot encoding
                        one_hot = torch.zeros(model.max_choices, device=self.device)
                        one_hot[sampled_class] = 1.0
                        
                        # Update input
                        temp_inputs[i, pos, 0] = 0
                        temp_inputs[i, pos, 1:1+model.max_choices] = one_hot
                
                # Compute loss with full supervision
                model.zero_grad()
                
                outputs = model(temp_inputs, annotators, questions, embeddings)
                batch_loss = model.compute_total_loss(
                    outputs, labels, temp_inputs, questions, embeddings,
                    full_supervision=True
                )
                
                if batch_loss > 0:
                    batch_loss.backward()
                    sample_count += 1
                    
                    for name, param in model.named_parameters():
                        if param.grad is not None:
                            if name not in temp_grad_dict:
                                temp_grad_dict[name] = param.grad.detach().clone()
                            else:
                                temp_grad_dict[name] += param.grad.detach().clone()
            
            if sample_count > 0:
                for name in temp_grad_dict:
                    temp_grad_dict[name] /= sample_count
                
                normalized_grad_dict = self.normalize_gradient(temp_grad_dict)
                grad_samples.append(normalized_grad_dict)
        
        return grad_samples
       

class GradientTopOnlySelector(GradientSelector):
    """
    Helper class for gradient-based selection.
    
    Computes and compares gradients for active learning,
    selecting examples that would provide the most training benefit.
    """
    
    def __init__(self, model, device=None):
        """
        Initialize gradient selector.
        
        Args:
            model: Model to use for predictions
            device: Device to use for computations
        """
        super().__init__(model, device)


    def _is_top_layer_param(self, param_name):
        """
        Helper method to identify if a parameter belongs to the top layer.
        Customize this method based on your model's architecture naming conventions.
        
        Args:
            param_name: Name of the parameter
            
        Returns:
            bool: True if the parameter belongs to the top layer, False otherwise
        """
        top_layer_identifiers = ['encoder.layers.5.out']
        return any(identifier in param_name.lower() for identifier in top_layer_identifiers)
    
    
    def compute_sample_gradient(self, model, inputs, labels, annotators, questions, embeddings):
        """
        Compute gradient for a single example using autoregressive sampling.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor
            labels: Label tensor
            annotators: Annotator indices
            questions: Question indices
            
        Returns:
            dict: Gradient dictionary
        """
        model.train()
        grad_dict = {}
        
        # Identify masked positions
        masked_positions = []
        for j in range(inputs.shape[1]):
            if inputs[0, j, 0] == 1:
                masked_positions.append(j)
        
        if not masked_positions:
            for name, param in model.named_parameters():
                if param.requires_grad:
                    grad_dict[name] = torch.zeros_like(param)
            return grad_dict
        
        temp_inputs = inputs.clone()
        temp_labels = labels.clone()
        
        for pos in masked_positions:
            with torch.no_grad():
                current_outputs = model(temp_inputs, annotators, questions, embeddings)
                var_outputs = current_outputs[0, pos]
                var_probs = F.softmax(var_outputs, dim=0)
            
            sampled_class = torch.multinomial(var_probs, 1).item()
            
            one_hot = torch.zeros(model.max_choices, device=self.device)
            one_hot[sampled_class] = 1.0
            
            temp_inputs[0, pos, 0] = 0
            temp_inputs[0, pos, 1:1+model.max_choices] = one_hot
            
            temp_labels[0, pos] = one_hot
        
        # Compute loss with full supervision
        model.zero_grad()
        
        outputs = model(temp_inputs, annotators, questions, embeddings)
        loss = model.compute_total_loss(
            outputs, temp_labels, temp_inputs, questions, embeddings,
            full_supervision=True
        )
        
        non_top_params = []
        for name, param in model.named_parameters():
            if not self._is_top_layer_param(name):
                if param.requires_grad:
                    param.requires_grad = False
                    non_top_params.append(param)

        model.zero_grad()
        outputs = model(temp_inputs, annotators, questions, embeddings)
        loss = model.compute_total_loss(
            outputs, temp_labels, temp_inputs, questions, embeddings,
            full_supervision=True
        )
        loss.backward()

        for param in non_top_params:
            param.requires_grad = True
        
        # Collect gradients
        for name, param in model.named_parameters():
            if param.requires_grad and param.grad is not None and self._is_top_layer_param(name):
                grad_dict[name] = param.grad.detach().clone()
        
        model.zero_grad()
        
        return grad_dict
    
    
    def compute_validation_gradient_sampled(self, model, val_dataloader, num_samples=5):
        """
        Compute validation gradients using sampling approach.
        
        Args:
            model: Model to use for predictions
            val_dataloader: Validation dataloader
            num_samples: Number of samples to compute
            
        Returns:
            list: List of gradient dictionaries
        """
        model.train()
        grad_samples = []
        
        for _ in tqdm(range(num_samples), desc="Computing validation gradients"):
            temp_grad_dict = {}
            sample_count = 0
            
            for batch in val_dataloader:
                known_questions, inputs, labels, annotators, questions, embeddings = batch
                inputs, labels, annotators, questions = (
                    inputs.to(self.device), labels.to(self.device), 
                    annotators.to(self.device), questions.to(self.device)
                )

                if embeddings is not None:
                    embeddings = embeddings.to(self.device)
                
                batch_size = inputs.shape[0]
                
                temp_inputs = inputs.clone()
                
                for i in range(batch_size):
                    masked_positions = []
                    for j in range(inputs.shape[1]):
                        if temp_inputs[i, j, 0] == 1:
                            masked_positions.append(j)
                    
                    # Sample values for masked positions
                    for pos in masked_positions:
                        with torch.no_grad():
                            current_outputs = model(temp_inputs, annotators, questions, embeddings)
                            var_outputs = current_outputs[i, pos]
                            var_probs = F.softmax(var_outputs, dim=0)
                        
                        # Sample a class
                        sampled_class = torch.multinomial(var_probs, 1).item()
                        
                        # Create one-hot encoding
                        one_hot = torch.zeros(model.max_choices, device=self.device)
                        one_hot[sampled_class] = 1.0
                        
                        # Update input
                        temp_inputs[i, pos, 0] = 0
                        temp_inputs[i, pos, 1:1+model.max_choices] = one_hot
                
                # Compute loss with full supervision
                model.zero_grad()

                non_top_params = []
                for name, param in model.named_parameters():
                    if not self._is_top_layer_param(name):
                        if param.requires_grad:
                            param.requires_grad = False
                            non_top_params.append(param)
                model.zero_grad()
                outputs = model(temp_inputs, annotators, questions, embeddings)
                batch_loss = model.compute_total_loss(
                    outputs, labels, temp_inputs, questions, embeddings,
                    full_supervision=True
                )
                
                if batch_loss > 0:
                    batch_loss.backward()
                    sample_count += 1
                    
                    for name, param in model.named_parameters():
                        if param.grad is not None and self._is_top_layer_param(name):
                            if name not in temp_grad_dict:
                                temp_grad_dict[name] = param.grad.detach().clone()
                            else:
                                temp_grad_dict[name] += param.grad.detach().clone()
                for param in non_top_params:
                    param.requires_grad = True
            
            if sample_count > 0:
                for name in temp_grad_dict:
                    temp_grad_dict[name] /= sample_count
                
                normalized_grad_dict = self.normalize_gradient(temp_grad_dict)
                grad_samples.append(normalized_grad_dict)
        
        return grad_samples


class GradientSelectionStrategy(ExampleSelectionStrategy):
    """
    Gradient-based example selection strategy for Active Learning.
    
    Selects examples that have gradient directions most aligned with
    the validation loss gradient, indicating they'd be most helpful
    for improving model performance on the validation set.
    """
    
    def __init__(self, model, device=None, gradient_top_only=False):
        """Initialize gradient selection strategy."""
        super().__init__("gradient", model, device)
        if gradient_top_only:
            self.selector = GradientTopOnlySelector(model, device)
        else:
            self.selector = GradientSelector(model, device)
    
    def select_examples(self, dataset, num_to_select=1, val_dataset=None, 
                        num_samples=5, batch_size=32, costs=None, **kwargs):
        """
        Select examples using gradient alignment.
        
        Args:
            dataset: Dataset to select from
            num_to_select: Number of examples to select
            val_dataset: Validation dataset
            num_samples: Number of samples to compute
            batch_size: Batch size for dataloaders
            costs: Dictionary mapping example indices to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            tuple: (Selected indices, Alignment scores)
        """
        if val_dataset is None:
            raise ValueError("Validation dataset is required for gradient selection")
        
        # Create validation dataloader
        val_dataloader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)
        
        # Compute validation gradients
        validation_grad_samples = self.selector.compute_validation_gradient_sampled(
            self.model, val_dataloader, num_samples=num_samples
        )
        
        # Calculate gradient alignment for each example
        all_scores = []
        all_indices = []
        all_costs = []
        all_bc_ratios = []
        
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False)
        
        for batch_idx, batch in enumerate(tqdm(dataloader, desc="Computing gradient alignment")):
            known_questions, inputs, labels, annotators, questions, embeddings = batch
            inputs, labels, annotators, questions = (
                inputs.to(self.device), labels.to(self.device), 
                annotators.to(self.device), questions.to(self.device)
            )
            if embeddings is not None:
                embeddings = embeddings.to(self.device)
            
            for i in range(inputs.shape[0]):
                # Skip examples with no masked positions
                if torch.all(inputs[i, :, 0] == 0).item():
                    continue
                    
                example_input = inputs[i:i+1]
                example_labels = labels[i:i+1]
                example_annotator = annotators[i:i+1]
                example_question = questions[i:i+1]
                example_embedding = embeddings[i:i+1]
                
                example_grad_dict = self.selector.compute_example_gradients(
                    self.model, 
                    example_input, example_labels, 
                    example_annotator, example_question, example_embedding,
                    num_samples=num_samples
                )
                
                if not example_grad_dict:
                    continue
                
                example_grad_dict = self.selector.normalize_gradient(example_grad_dict)
                
                alignment_scores = []
                for val_grad in validation_grad_samples:
                    alignment = self.selector.compute_grad_dot_product(val_grad, example_grad_dict)
                    alignment_scores.append(alignment)
                
                global_idx = batch_idx * dataloader.batch_size + i
                
                # Get cost for this example
                cost = 1.0  # Default cost
                if costs and global_idx in costs:
                    cost = costs[global_idx]
                
                avg_alignment = sum(alignment_scores) / len(alignment_scores) if alignment_scores else 0.0
                benefit_cost_ratio = avg_alignment / max(cost, 1e-10)
                
                all_scores.append(avg_alignment)
                all_indices.append(global_idx)
                all_costs.append(cost)
                all_bc_ratios.append(benefit_cost_ratio)
        
        if all_scores:
            # Sort by benefit/cost ratio or alignment score
            if kwargs.get('use_benefit_cost_ratio', True):
                sorted_data = sorted(zip(all_indices, all_scores, all_costs, all_bc_ratios), 
                                    key=lambda x: x[3], reverse=True)
                sorted_indices = [idx for idx, _, _, _ in sorted_data]
                sorted_scores = [score for _, score, _, _ in sorted_data]
            else:
                sorted_data = sorted(zip(all_indices, all_scores, all_costs, all_bc_ratios), 
                                    key=lambda x: x[1], reverse=True)
                sorted_indices = [idx for idx, _, _, _ in sorted_data]
                sorted_scores = [score for _, score, _, _ in sorted_data]
            
            selected_indices = sorted_indices[:num_to_select]
            selected_scores = sorted_scores[:num_to_select]
        else:
            selected_indices = []
            selected_scores = []
        
        return selected_indices, selected_scores

class EntropyExampleSelectionStrategy(ExampleSelectionStrategy):
    """
    Entropy-based example selection strategy for Active Learning.
    
    Selects examples with the highest predictive entropy, indicating
    the model is most uncertain about these examples.
    """
    
    def __init__(self, model, device=None):
        """Initialize entropy example selection strategy."""
        super().__init__("entropy", model, device)
    
    def select_examples(self, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Select examples with highest prediction entropy.
        
        Args:
            dataset: Dataset to select from
            num_to_select: Number of examples to select
            costs: Dictionary mapping example indices to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            list: Indices of selected examples
            list: Entropy scores for selected examples
        """
        self.model.eval()
        
        # Calculate entropy for all examples
        entropies = []
        valid_indices = []
        
        for idx in range(len(dataset)):
            masked_positions = dataset.get_masked_positions(idx)
            if not masked_positions:
                continue
                
            valid_indices.append(idx)
            
            # Get data for this example
            known_questions, inputs, answers, annotators, questions, embeddings = dataset[idx]
            inputs = inputs.unsqueeze(0).to(self.device)
            answers = answers.unsqueeze(0).to(self.device)
            annotators = annotators.unsqueeze(0).to(self.device)
            questions = questions.unsqueeze(0).to(self.device)

            if embeddings is not None:
                embeddings = embeddings.unsqueeze(0).to(self.device)
            
            # Make predictions
            with torch.no_grad():
                outputs = self.model(inputs, annotators, questions, embeddings)
                
                # Calculate entropy for all masked positions
                total_entropy = 0.0
                count = 0
                
                for pos in masked_positions:
                    # Get probabilities for this position
                    logits = outputs[0, pos]
                    probs = F.softmax(logits, dim=0)
                    
                    # Calculate entropy: -sum(p_i * log(p_i))
                    entropy = -torch.sum(probs * torch.log(probs + 1e-10)).item()
                    total_entropy += entropy
                    count += 1
                
                # Average entropy across all masked positions
                avg_entropy = total_entropy / max(1, count)
                entropies.append(avg_entropy)
        
        if not valid_indices:
            return [], []
            
        # Adjust for costs if provided
        if costs:
            adjusted_scores = []
            for i, idx in enumerate(valid_indices):
                cost = costs.get(idx, 1.0)
                adjusted_scores.append(entropies[i] / max(cost, 1e-10))
                
            # Sort by adjusted scores
            sorted_indices = [x for _, x in sorted(zip(adjusted_scores, valid_indices), reverse=True)]
            sorted_scores = sorted(adjusted_scores, reverse=True)
        else:
            # Sort by entropy
            sorted_indices = [x for _, x in sorted(zip(entropies, valid_indices), reverse=True)]
            sorted_scores = sorted(entropies, reverse=True)
        
        # Select top examples
        selected_indices = sorted_indices[:num_to_select]
        selected_scores = sorted_scores[:num_to_select]
        
        return selected_indices, selected_scores


class EntropyFeatureSelectionStrategy(FeatureSelectionStrategy):
    """
    Entropy-based feature selection strategy for Active Feature Acquisition.
    
    Selects features with the highest predictive entropy, indicating
    the model is most uncertain about these features.
    """
    
    def __init__(self, model, device=None):
        """Initialize entropy feature selection strategy."""
        super().__init__("entropy", model, device)
    
    def select_features(self, example_idx, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Select features with highest prediction entropy within an example.
        
        Args:
            example_idx: Index of the example to select features from
            dataset: Dataset containing the example
            num_to_select: Number of features to select
            costs: Dictionary mapping positions to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            list: Tuples of (position_idx, entropy, cost, entropy/cost_ratio) for selected positions
        """
        self.model.eval()
        
        # Get masked positions for this example
        masked_positions = dataset.get_masked_positions(example_idx)
        
        if not masked_positions:
            return []
            
        # Get data for this example
        known_questions, inputs, answers, annotators, questions, embeddings = dataset[example_idx]
        inputs = inputs.unsqueeze(0).to(self.device)
        answers = answers.unsqueeze(0).to(self.device)
        annotators = annotators.unsqueeze(0).to(self.device)
        questions = questions.unsqueeze(0).to(self.device)
        if embeddings is not None:
            embeddings = embeddings.unsqueeze(0).to(self.device)
        
        # Make predictions
        with torch.no_grad():
            outputs = self.model(inputs, annotators, questions, embeddings)
            
            # Calculate entropy for each masked position
            position_entropies = []
            
            for position in masked_positions:
                # Get probabilities for this position
                logits = outputs[0, position]
                probs = F.softmax(logits, dim=0)
                
                # Calculate entropy: -sum(p_i * log(p_i))
                entropy = -torch.sum(probs * torch.log(probs + 1e-10)).item()
                
                # Get cost for this position
                cost = 1.0  # Default cost
                if costs and position in costs:
                    cost = costs[position]
                    
                # Calculate benefit/cost ratio
                ratio = entropy / max(cost, 1e-10)
                
                position_entropies.append((position, entropy, cost, ratio))
        
        # Sort by entropy/cost ratio (highest first)
        position_entropies.sort(key=lambda x: x[3], reverse=True)
        
        # Return top selections
        return position_entropies[:num_to_select]

class CombinedSelectionStrategy:
    """
    Combines example selection and feature selection strategies.
    
    This allows for a two-stage selection process: first selecting
    the most promising examples, then identifying the most valuable
    features within those examples.
    """
    
    def __init__(self, example_strategy, feature_strategy):
        """
        Initialize combined selection strategy.
        
        Args:
            example_strategy: Strategy for selecting examples
            feature_strategy: Strategy for selecting features within examples
        """
        self.example_strategy = example_strategy
        self.feature_strategy = feature_strategy
        self.name = f"{example_strategy.name}+{feature_strategy.name}"
    
    def select(self, dataset, num_examples=1, num_features=1, example_costs=None, feature_costs=None, **kwargs):
        """
        Select examples and then features within those examples.
        
        Args:
            dataset: Dataset to select from
            num_examples: Number of examples to select
            num_features: Number of features to select per example
            example_costs: Dictionary mapping example indices to their costs
            feature_costs: Dictionary mapping (example_idx, position_idx) to costs
            **kwargs: Additional arguments
            
        Returns:
            list: Tuples of (example_idx, position_idx, benefit, cost, benefit/cost_ratio) for selected features
        """
        # First select examples
        example_result = self.example_strategy.select_examples(
            dataset, num_examples, costs=example_costs, **kwargs
        )
        
        if isinstance(example_result, tuple):
            # Handle case where example strategy returns (indices, scores)
            example_indices, example_scores = example_result
        else:
            example_indices = example_result
            example_scores = [1.0] * len(example_indices)  # Default scores
        
        # Then select features within each example
        selections = []
        for i, example_idx in enumerate(example_indices):
            # Get costs for positions in this example
            if feature_costs and example_idx in feature_costs:
                pos_costs = feature_costs[example_idx]
            else:
                pos_costs = None
                
            # Include example score as additional argument
            kwargs['example_score'] = example_scores[i] if i < len(example_scores) else 1.0
            
            feature_selections = self.feature_strategy.select_features(
                example_idx, dataset, num_features, costs=pos_costs, **kwargs
            )
            
            # Process selections based on different strategy return formats
            for selection in feature_selections:
                if isinstance(selection, tuple):
                    if len(selection) >= 4:  # (position, benefit, cost, ratio, [class])
                        position, benefit, cost, ratio = selection[:4]
                        extra_data = selection[4:] if len(selection) > 4 else []
                        selections.append((example_idx, position, benefit, cost, ratio) + tuple(extra_data))
                    elif len(selection) == 2:  # (position, score)
                        position, score = selection
                        cost = 1.0  # Default cost
                        if pos_costs and position in pos_costs:
                            cost = pos_costs[position]
                        ratio = score / max(cost, 1e-10)
                        selections.append((example_idx, position, score, cost, ratio))
                else:  # Just position
                    position = selection
                    cost = 1.0  # Default cost
                    if pos_costs and position in pos_costs:
                        cost = pos_costs[position]
                    selections.append((example_idx, position, cost, cost, 1.0))
        
        # Sort by benefit/cost ratio (highest first)
        selections.sort(key=lambda x: x[4], reverse=True)
        
        return selections

class BADGESelectionStrategy(ExampleSelectionStrategy):
    """
    BADGE (Batch Active learning by Diverse Gradient Embeddings) example selection strategy.
    
    This strategy selects examples based on gradient embeddings diversity, combining
    the benefits of uncertainty sampling and diversity sampling.
    """
    
    def __init__(self, model, device=None):
        """Initialize BADGE selection strategy."""
        super().__init__("badge", model, device)
    
    def select_examples(self, dataset, num_to_select=1, costs=None, **kwargs):
        """
        Select examples using BADGE strategy.
        
        Args:
            dataset: Dataset to select from
            num_to_select: Number of examples to select
            costs: Dictionary mapping example indices to their annotation costs
            **kwargs: Additional arguments specific to the strategy
            
        Returns:
            list: Indices of selected examples
            list: Corresponding scores for selected examples
        """
        self.model.eval()
        
        # Get all valid examples (with at least one masked position)
        valid_indices = []
        example_datas = []
        
        for idx in range(len(dataset)):
            masked_positions = dataset.get_masked_positions(idx)
            if masked_positions:
                valid_indices.append(idx)
                known_questions, inputs, answers, annotators, questions, embeddings = dataset[idx]
                example_datas.append((inputs, answers, annotators, questions, embeddings))
        
        if not valid_indices:
            return [], []
        
        # Compute gradient embeddings for all valid examples
        embeddings = []
        scores = []  # Will use uncertainty as scores
        
        for i, idx in enumerate(valid_indices):
            inputs, answers, annotators, questions, question_embeddings = example_datas[i]
            inputs = inputs.to(self.device)
            answers = answers.to(self.device)
            annotators = annotators.to(self.device)
            questions = questions.to(self.device)

            if embeddings is not None:
                question_embeddings = question_embeddings.unsqueeze(0).to(self.device)
            
            # Compute hypothetical labels and gradient embeddings
            embedding, uncertainty_score = self.compute_gradient_embedding(self.model, inputs.unsqueeze(0), answers.unsqueeze(0), annotators.unsqueeze(0), questions.unsqueeze(0), question_embeddings)
            
            embeddings.append(embedding)
            scores.append(uncertainty_score)
        
        embeddings = np.vstack(embeddings)
        
        # Select examples using k-means++ seeding
        selected_indices = self.kmeans_plus_plus_sampling(embeddings, min(num_to_select, len(valid_indices)))
        
        # Map selected indices back to original dataset indices
        selected_examples = [valid_indices[i] for i in selected_indices]
        selected_scores = [scores[i] for i in selected_indices]
        
        # Adjust for costs if provided
        if costs:
            # Compute benefit/cost for each example
            benefit_cost_pairs = []
            for i, idx in enumerate(selected_examples):
                cost = costs.get(idx, 1.0)
                benefit_cost_ratio = selected_scores[i] / max(cost, 1e-10)
                benefit_cost_pairs.append((idx, selected_scores[i], benefit_cost_ratio))
            
            # Sort by benefit/cost ratio
            benefit_cost_pairs.sort(key=lambda x: x[2], reverse=True)
            
            # Extract sorted indices and scores
            selected_examples = [idx for idx, _, _ in benefit_cost_pairs]
            selected_scores = [score for _, score, _ in benefit_cost_pairs]
        
        return selected_examples, selected_scores
    
    def compute_gradient_embedding(self, model, inputs, labels, annotators, questions, question_embeddings):
        """
        Compute gradient embedding vector for an example.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor [batch_size, sequence_length, input_dim]
            labels: Label tensor
            annotators: Annotator indices
            questions: Question indices
            
        Returns:
            tuple: (gradient_embedding, uncertainty_score)
        """
        model.eval()
        
        # 1. Identify masked positions
        masked_positions = []
        for j in range(inputs.shape[1]):
            if inputs[0, j, 0] == 1:
                masked_positions.append(j)
        
        if not masked_positions:
            # No masked positions, return zero vector
            return np.zeros(model.max_choices * len(masked_positions)), 0.0
        
        # 2. For each masked position, compute hypothetical label and gradient embedding
        with torch.no_grad():
            outputs = model(inputs, annotators, questions, question_embeddings)
        
        all_position_embeddings = []
        total_uncertainty = 0.0
        
        # Enable gradients for embedding computation
        for pos in masked_positions:
            # Get predicted class (hypothetical label)
            logits = outputs[0, pos]
            probs = F.softmax(logits, dim=0)
            pred_class = torch.argmax(probs).item()
            
            # Measure uncertainty (entropy)
            entropy = -torch.sum(probs * torch.log(probs + 1e-10)).item()
            total_uncertainty += entropy
            
            # Create target using the class index
            with torch.enable_grad():
                model.zero_grad()
                
                # Forward pass with requires_grad
                inputs_clone = inputs.clone().detach()
                inputs_clone.requires_grad_(True)
                outputs_grad = model(inputs_clone, annotators, questions, question_embeddings)
                
                # Add batch dimension to logits and ensure target has correct shape
                logits = outputs_grad[0, pos].unsqueeze(0)  # Shape: [1, num_classes]
                target = torch.tensor([pred_class], device=self.device)  # Shape: [1]
                
                # Compute loss
                loss = F.cross_entropy(logits, target)
                loss.backward()
                
                # Extract gradient with respect to output layer weights
                output_layer_grads = []
                for name, param in model.named_parameters():
                    if 'encoder.param_update' in name or 'encoder.layers' in name and 'out' in name:
                        if param.grad is not None:
                            flat_grad = param.grad.detach().view(-1)
                            output_layer_grads.append(flat_grad)
                
                if output_layer_grads:
                    position_embedding = torch.cat(output_layer_grads).cpu().numpy()
                    # Normalize the embedding
                    norm = np.linalg.norm(position_embedding)
                    if norm > 0:
                        position_embedding = position_embedding / norm
                    all_position_embeddings.append(position_embedding)
        
        # Combine all position embeddings to get example embedding
        if all_position_embeddings:
            final_embedding = np.concatenate(all_position_embeddings)
            avg_uncertainty = total_uncertainty / len(masked_positions)
            return final_embedding, avg_uncertainty
        else:
            # Fallback if no gradients were collected
            return np.zeros(10), 0.0
    
    def kmeans_plus_plus_sampling(self, embeddings, n_samples):
        """
        Implements k-means++ seeding algorithm for diversity sampling.
        
        Args:
            embeddings: Numpy array of embeddings [n_examples, embedding_dim]
            n_samples: Number of examples to select
            
        Returns:
            list: Indices of selected examples
        """
        n_examples = embeddings.shape[0]
        
        # If we need all examples or just one, return all indices
        if n_samples >= n_examples or n_samples <= 1:
            return list(range(min(n_samples, n_examples)))
        
        # Select first example randomly
        selected_indices = [random.randint(0, n_examples - 1)]
        selected_embeddings = embeddings[selected_indices]
        
        # Select remaining examples
        for _ in range(1, n_samples):
            # Compute distances to nearest selected example
            distances = pairwise_distances(
                embeddings, selected_embeddings, metric='euclidean'
            ).min(axis=1)
            
            # Normalize distances as probabilities (squared distances for k-means++)
            probabilities = distances**2 / np.sum(distances**2)
            
            # Sample next example based on these probabilities
            next_idx = np.random.choice(n_examples, 1, p=probabilities)[0]
            
            selected_indices.append(next_idx)
            selected_embeddings = embeddings[selected_indices]
        
        return selected_indices


class ArgmaxVOICalculator(VOICalculator):
    """
    Value of Information (VOI) calculator using argmax instead of expectation.
    
    Instead of computing expected reduction in loss over all possible values,
    this only considers the most likely value (argmax) of the candidate variable.
    """
    
    def __init__(self, model, device=None):
        """Initialize ArgmaxVOI calculator."""
        super().__init__(model, device)
    
    def compute_argmax_voi(self, model, inputs, annotators, questions, known_questions, embeddings, candidate_idx, target_indices, loss_type="cross_entropy", cost=1.0):
        """
        Compute VOI using only the argmax value instead of expectation.
        
        Args:
            model: Model to use for predictions
            inputs: Input tensor [batch_size, sequence_length, input_dim]
            annotators: Annotator indices [batch_size, sequence_length]
            questions: Question indices [batch_size, sequence_length]
            known_questions: Mask indicating known questions [batch_size, sequence_length]
            candidate_idx: Index of candidate annotation to evaluate
            target_indices: Target indices to compute loss on
            loss_type: Type of loss to compute
            cost: Cost of annotating this position
            
        Returns:
            tuple: (voi_value, voi/cost_ratio, expected_posterior_loss)
        """
        model.eval()

        with torch.no_grad():
            # Get initial outputs and compute initial loss
            outputs = model(inputs, annotators, questions, embeddings)
            
            # Extract predictions for target indices
            if isinstance(target_indices, list) and len(target_indices) == 1:
                target_idx = target_indices[0]
                target_preds = outputs[:, target_idx, :]
            else:
                # Handle multiple target indices - concatenate predictions
                target_preds = torch.cat([outputs[:, idx, :].unsqueeze(1) for idx in target_indices], dim=1)
            
            # Compute initial loss
            loss_initial = self.compute_loss(target_preds, loss_type)
            
            # Get prediction for candidate
            candidate_pred = outputs[:, candidate_idx, :]
            candidate_probs = F.softmax(candidate_pred, dim=-1)
            
            # Get most likely class (argmax)
            most_likely_class = torch.argmax(candidate_probs, dim=1)[0].item()
            
            batch_size = inputs.shape[0]
            num_classes = candidate_probs.shape[-1]
            
            # Create copy of inputs with candidate set to most likely class
            input_with_answer = inputs.clone()
            one_hot = F.one_hot(torch.tensor(most_likely_class), num_classes=num_classes).float().to(self.device)
            input_with_answer[:, candidate_idx, 1:] = one_hot
            input_with_answer[:, candidate_idx, 0] = 0  # Mark as observed
            
            # Get predictions with argmax value
            new_outputs = model(input_with_answer, annotators, questions, embeddings)
            
            # Extract target predictions
            if isinstance(target_indices, list) and len(target_indices) == 1:
                target_idx = target_indices[0]
                new_target_preds = new_outputs[:, target_idx, :]
            else:
                # Handle multiple target indices
                new_target_preds = torch.cat([new_outputs[:, idx, :].unsqueeze(1) for idx in target_indices], dim=1)
            
            # Compute posterior loss with argmax value
            posterior_loss = self.compute_loss(new_target_preds, loss_type)
            
            # VOI is the reduction in loss
            voi = loss_initial - posterior_loss
            
            # Compute benefit/cost ratio
            voi_cost_ratio = voi / max(cost, 1e-10)
            
            return voi, voi_cost_ratio, posterior_loss


class ArgmaxVOISelectionStrategy(FeatureSelectionStrategy):
    """
    VOI-based feature selection strategy that only considers the argmax value.
    
    Uses the ArgmaxVOICalculator to compute VOI more efficiently by only
    considering the most likely value for each candidate variable.
    """
    
    def __init__(self, model, device=None):
        """Initialize ArgmaxVOI selection strategy."""
        super().__init__("voi_argmax", model, device)
        self.voi_calculator = ArgmaxVOICalculator(model, device)
    
    def select_features(self, example_idx, dataset, num_to_select=1, target_questions=None, 
                       loss_type="cross_entropy", costs=None, **kwargs):
        """
        Select features (positions) using ArgmaxVOI within a given example.
        
        Args:
            example_idx: Index of the example to select features from
            dataset: Dataset containing the example
            num_to_select: Number of features to select
            target_questions: Target questions to compute VOI for
            loss_type: Type of loss to compute
            costs: Dictionary mapping positions to their annotation costs
            **kwargs: Additional arguments
            
        Returns:
            list: Tuples of (position_idx, benefit, cost, benefit/cost_ratio) for selected positions
        """
        if target_questions is None:
            # Default to first question (Q0)
            target_questions = [0]
        
        # Convert target questions to indices if needed
        if isinstance(target_questions[0], str):
            question_list = ['Q0', 'Q1', 'Q2', 'Q3', 'Q4', 'Q5', 'Q6']
            target_questions = [question_list.index(q) for q in target_questions if q in question_list]
        
        # Get masked positions
        masked_positions = dataset.get_masked_positions(example_idx)
        if not masked_positions:
            return []
        
        # Get data
        known_questions, inputs, answers, annotators, questions, embeddings = dataset[example_idx]
        inputs = inputs.unsqueeze(0).to(self.device)
        answers = answers.unsqueeze(0).to(self.device)
        annotators = annotators.unsqueeze(0).to(self.device)
        questions = questions.unsqueeze(0).to(self.device)
        known_questions = known_questions.unsqueeze(0).to(self.device)
        if embeddings is not None:
            embeddings = embeddings.unsqueeze(0).to(self.device)
        
        target_indices = []
        for q_idx in target_questions:
            for i in range(questions.shape[1]):
                if (questions[0, i].item() == q_idx and 
                    not dataset.is_position_noisy(example_idx, i)):
                    target_indices.append(i)
        
        if not target_indices:
            return []
        
        # Calculate ArgmaxVOI for each masked position
        position_vois = []
        for position in masked_positions:
            # Get cost for this position
            cost = 1.0  # Default cost
            if costs and position in costs:
                cost = costs[position]
                
            # Compute ArgmaxVOI
            voi, voi_cost_ratio, posterior_loss = self.voi_calculator.compute_argmax_voi(
                self.model, inputs, annotators, questions, known_questions, embeddings,
                position, target_indices, loss_type, cost=cost
            )
            
            position_vois.append((position, voi, cost, voi_cost_ratio))
        
        # Sort by benefit/cost ratio (highest first)
        position_vois.sort(key=lambda x: x[3], reverse=True)
        
        # Return top selections
        return position_vois[:num_to_select]


class SelectionFactory:
    """
    Factory for creating selection strategies.
    
    Provides a centralized way to instantiate different
    selection strategies with consistent configuration.
    """
    
    @staticmethod
    def create_example_strategy(strategy_name, model, device=None, gradient_top_only=False):
        """
        Create example selection strategy.
        
        Args:
            strategy_name: Name of the strategy
            model: Model to use for predictions
            device: Device to use for computations
            gradient_top_only: Whether to use only top layer gradients (for GradientSelectionStrategy)
            
        Returns:
            ExampleSelectionStrategy: Example selection strategy
        """
        if strategy_name == "random":
            return RandomExampleSelectionStrategy(model, device)
        elif strategy_name == "gradient":
            return GradientSelectionStrategy(model, device, gradient_top_only=gradient_top_only)
        elif strategy_name == "entropy":
            return EntropyExampleSelectionStrategy(model, device)
        elif strategy_name == "badge":
            return BADGESelectionStrategy(model, device)
        else:
            raise ValueError(f"Unknown example selection strategy: {strategy_name}")
    
    @staticmethod
    def create_feature_strategy(strategy_name, model, device=None):
        """
        Create feature selection strategy.
        
        Args:
            strategy_name: Name of the strategy
            model: Model to use for predictions
            device: Device to use for computations
            
        Returns:
            FeatureSelectionStrategy: Feature selection strategy
        """
        if strategy_name == "random":
            return RandomFeatureSelectionStrategy(model, device)
        elif strategy_name == "voi":
            return VOISelectionStrategy(model, device)
        elif strategy_name == "fast_voi":
            return FastVOISelectionStrategy(model, device)
        elif strategy_name == "voi_argmax":
            return ArgmaxVOISelectionStrategy(model, device)
        elif strategy_name == "sequential":
            return RandomFeatureSelectionStrategy(model, device)
        elif strategy_name == "entropy":
            return EntropyFeatureSelectionStrategy(model, device)
        else:
            raise ValueError(f"Unknown feature selection strategy: {strategy_name}")
    
    @staticmethod
    def create_combined_strategy(example_strategy_name, feature_strategy_name, model, device=None):
        """
        Create combined selection strategy.
        
        Args:
            example_strategy_name: Name of the example selection strategy
            feature_strategy_name: Name of the feature selection strategy
            model: Model to use for predictions
            device: Device to use for computations
            
        Returns:
            CombinedSelectionStrategy: Combined selection strategy
        """
        example_strategy = SelectionFactory.create_example_strategy(example_strategy_name, model, device)
        feature_strategy = SelectionFactory.create_feature_strategy(feature_strategy_name, model, device)
        return CombinedSelectionStrategy(example_strategy, feature_strategy)